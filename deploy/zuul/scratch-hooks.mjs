// Check rendered PodTemplates, not just source snippets: kustomize must preserve the hook contract.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const hookPath = "/usr/local/libexec/aid-scratch-control";
export function checkScratchHook(template, required) {
  if (template.kind !== "PodTemplate") throw new Error("expected rendered PodTemplate");
  const spec = template.template.spec;
  const volumes = spec.volumes || [];
  const node = spec.containers.find(c => c.name === "node");
  const mounts = (node?.volumeMounts || []).filter(m => m.mountPath === hookPath);
  const hooks = volumes.filter(v => v.configMap?.name === "aid-scratch-control");
  if (!required) {
    if (mounts.length || hooks.length) throw new Error("scratch hook on a non-image node");
    return;
  }
  if (hooks.length !== 1 || mounts.length !== 1) throw new Error("scratch node needs one hook volume/mount");
  const volume = hooks[0], mount = mounts[0], cm = volume.configMap;
  if (cm.optional || cm.defaultMode !== 0o555 || cm.items) throw new Error("hook ConfigMap must be required, executable 0555, and preserve its keys");
  if (mount.name !== volume.name || mount.subPath !== "aid-scratch-control" || mount.subPathExpr || mount.readOnly !== true) {
    throw new Error("hook mount must be read-only with the exact file subPath");
  }
  if (spec.containers.filter(c => c !== node).some(c => (c.volumeMounts || []).some(m => m.name === volume.name))) {
    throw new Error("scratch hook mounted in an unintended container");
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [gate, post, check, compute] = process.argv.slice(2);
    for (const [file, required] of [[gate,true],[post,true],[check,false],[compute,false]]) {
      checkScratchHook(JSON.parse(readFileSync(file, "utf8")), required);
    }
    console.log("Rendered scratch hooks: gate/post executable subPath; check/compute excluded");
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
