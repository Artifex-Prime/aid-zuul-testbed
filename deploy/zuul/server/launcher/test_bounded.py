"""Run with the pinned Zuul 14.3 runtime; no cluster or credentials required."""
import unittest
from types import SimpleNamespace as S
from unittest.mock import Mock, patch

from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
from zuul.driver.kubernetes import KubernetesDriver
from zuul.driver.kubernetes.kubernetesmodel import KubernetesProviderNode
import bounded


def pod(name='np0123456789abc', ready=True, phase='Running', labels=None):
    return S(metadata=S(name=name, labels=labels or {}, deletion_timestamp=None),
             status=S(pod_ip='10.42.0.5', phase=phase,
                      conditions=[S(type='Ready', status='True' if ready else 'False')]))


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.api_client = ApiClient()
        self.template = {'metadata': {'labels': {'app.kubernetes.io/name': 'zuul-node'}},
                         'spec': {'restartPolicy': 'Never', 'containers': [
                             {'name': 'node', 'image': 'registry/node@sha256:' + 'a' * 64}]}}
        self.api.read_namespaced_pod_template.return_value = S(template=self.template)
        self.endpoint = S(connection=S(connection_config={'namespace': 'zuul-nodes'}), core_client=self.api)
        self.node = S(uuid='0123456789abcdef', create_state={}, delete_state={}, kubernetes_namespace_id=None,
                      tags={'zuul_node_uuid': '0123456789abcdef'}, updateFromInstance=Mock())
        self.label, self.image = S(kind='pod'), S(connection_type='ssh')
        self.api.read_namespaced_pod.return_value = pod(labels=self.node.tags)

    def create(self):
        return bounded.Create(self.endpoint, self.node, 'np0123456789abc', self.label, None, self.image, None)

    def test_ready_node_uses_only_admin_template_and_no_namespace_or_rbac_calls(self):
        for ns in ('zuul-nodes', 'zuul-gate', 'zuul-post'):
            with self.subTest(namespace=ns):
                self.api.reset_mock()
                self.endpoint.connection.connection_config['namespace'] = ns
                machine = self.create()
                machine.advance()
                self.assertTrue(machine.complete)
                self.assertEqual(self.node.kubernetes_namespace_id, 'np0123456789abc')
                self.api.read_namespaced_pod_template.assert_called_once_with('job-node', ns)
                args = self.api.create_namespaced_pod.call_args.args
                self.assertEqual(args[0], ns)
                self.assertEqual(args[1]['spec'], self.template['spec'])
                self.assertEqual(args[1]['metadata']['labels']['zuul_node_uuid'], self.node.tags['zuul_node_uuid'])
                self.assertEqual([c[0] for c in self.api.mock_calls], [
                    'read_namespaced_pod_template', 'create_namespaced_pod', 'read_namespaced_pod'])
                self.assertEqual(self.node.updateFromInstance.call_args.args[0].interface_ip, '10.42.0.5')

    def test_waits_for_ssh_readiness_without_recreating(self):
        self.api.read_namespaced_pod.return_value = pod(ready=False)
        machine = self.create()
        machine.advance()
        self.assertFalse(machine.complete)
        self.node.updateFromInstance.assert_not_called()
        self.api.read_namespaced_pod.return_value = pod()
        machine.advance()
        self.assertTrue(machine.complete)
        self.api.create_namespaced_pod.assert_called_once()

    def test_completed_or_failed_pod_is_not_a_usable_node(self):
        for phase in ('Succeeded', 'Failed'):
            self.api.read_namespaced_pod.return_value = pod(phase=phase)
            with self.assertRaisesRegex(RuntimeError, 'ended before readiness'):
                self.create().advance()

    def test_launch_retry_adopts_only_its_own_pod(self):
        self.api.create_namespaced_pod.side_effect = ApiException(status=409)
        machine = self.create()
        machine.advance()
        self.assertTrue(machine.complete)
        self.api.read_namespaced_pod.return_value = pod(labels={'zuul_node_uuid': 'another'})
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.create().advance()

    def test_delete_waits_until_pod_is_gone_and_survives_restart(self):
        self.node.kubernetes_namespace_id = 'np0123456789abc'
        machine = bounded.Delete(self.endpoint, self.node, None)
        machine.advance()
        self.assertFalse(machine.complete)
        self.api.delete_namespaced_pod.assert_called_once_with('np0123456789abc', 'zuul-nodes')
        self.node.delete_state = machine.toDict()
        resumed = bounded.Delete(self.endpoint, self.node, None)
        self.api.read_namespaced_pod.side_effect = ApiException(status=404)
        resumed.advance()
        self.assertTrue(resumed.complete)
        self.api.delete_namespaced_pod.assert_called_once()

    def test_failed_launch_still_has_a_deletable_identity(self):
        self.api.create_namespaced_pod.side_effect = ApiException(status=503)
        with self.assertRaises(ApiException):
            self.create().advance()
        self.assertEqual(self.node.kubernetes_namespace_id, 'np0123456789abc')
        self.api.delete_namespaced_pod.side_effect = ApiException(status=404)
        self.api.read_namespaced_pod.side_effect = ApiException(status=404)
        machine = bounded.Delete(self.endpoint, self.node, None)
        machine.advance()
        self.assertTrue(machine.complete)

    def test_delete_after_uncertain_create_reconstructs_identity(self):
        self.api.read_namespaced_pod.side_effect = ApiException(status=404)
        machine = bounded.Delete(self.endpoint, self.node, None)
        machine.advance()
        self.api.delete_namespaced_pod.assert_called_once_with('np0123456789abcdef', 'zuul-nodes')
        self.assertTrue(machine.complete)

    def test_api_failures_are_not_deletion_success(self):
        self.node.kubernetes_namespace_id = 'np0123456789abc'
        self.api.delete_namespaced_pod.side_effect = ApiException(status=403)
        with self.assertRaises(ApiException):
            bounded.Delete(self.endpoint, self.node, None).advance()

    def test_namespace_kind_connection_and_unpinned_images_fail_closed(self):
        self.endpoint.connection.connection_config['namespace'] = 'zuul'
        with self.assertRaises(ValueError):
            self.create()
        self.endpoint.connection.connection_config['namespace'] = 'zuul-nodes'
        self.label.kind = 'namespace'
        with self.assertRaises(ValueError):
            self.create()
        self.label.kind = 'pod'
        self.image.connection_type = 'kubectl'
        with self.assertRaises(ValueError):
            self.create()
        self.image.connection_type = 'ssh'
        self.template['spec']['containers'][0]['image'] = 'registry/node:latest'
        with self.assertRaisesRegex(ValueError, 'pinned by digest'):
            self.create().advance()
        self.api.create_namespaced_pod.assert_not_called()

    def test_leaks_are_pods_and_collection_never_deletes_a_namespace(self):
        self.api.list_namespaced_pod.return_value = S(items=[pod(labels=self.node.tags)])
        resources = list(bounded.Endpoint.listResources(self.endpoint, []))
        self.assertEqual(resources[0].metadata, self.node.tags)
        self.assertEqual(resources[0].type, 'pod')
        bounded.Endpoint.deleteResource(self.endpoint, resources[0])
        self.api.delete_namespaced_pod.assert_called_once_with('np0123456789abc', 'zuul-nodes')
        instances = list(bounded.Endpoint.listInstances(self.endpoint))
        self.assertEqual(instances[0].external_id, 'pod=np0123456789abc')
        self.assertEqual(instances[0].getQuotaInformation().getResources()['pods'], 1)

    def test_factory_is_compatible_with_pinned_upstream_and_node_driver_data(self):
        bounded.install()
        self.assertIs(KubernetesDriver._endpoint_class, bounded.Endpoint)
        node = KubernetesProviderNode()
        node._set(kubernetes_namespace_id='np0123456789abc')
        self.assertEqual(node.getDriverData(), {'kubernetes_namespace_id': 'np0123456789abc'})
        driver = KubernetesDriver()
        with patch('zuul.provider.QuotaCache'):
            endpoint = driver._getEndpoint(None, S(connection_name='bounded', connection_config={'namespace': 'zuul-nodes'}), 'system')
        self.assertIsInstance(endpoint, bounded.Endpoint)
        self.assertIs(endpoint.driver, driver)


if __name__ == '__main__':
    unittest.main(verbosity=2)
