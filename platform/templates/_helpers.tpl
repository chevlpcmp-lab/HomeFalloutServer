{{- define "platform.wave" -}}
{{- $c := .component -}}
{{- if not (hasKey .root.Values.tiers $c.tier) -}}
{{- fail (printf "component %q uses unknown tier %q" $c.name $c.tier) -}}
{{- end -}}
{{- add (int (index .root.Values.tiers $c.tier)) (int ($c.waveOffset | default 0)) (int (.phase | default 0)) -}}
{{- end -}}

{{- define "platform.bool" -}}
{{- if hasKey .ctx .key -}}
{{- ternary "true" "" (index .ctx .key) -}}
{{- else -}}
{{- ternary "true" "" .default -}}
{{- end -}}
{{- end -}}

{{- define "platform.project" -}}
{{- default .root.Values.defaults.project .component.project -}}
{{- end -}}

{{- define "platform.syncPolicy" -}}
syncPolicy:
  automated:
    prune: true
    selfHeal: true
  syncOptions:
    - CreateNamespace=true
    - ServerSideApply=true
    - SkipDryRunOnMissingResource=true
    - RespectIgnoreDifferences=true
  retry:
    limit: 5
    backoff:
      duration: 5s
      factor: 2
      maxDuration: 3m
{{- end -}}

{{- define "platform.ignoreDifferences" -}}
ignoreDifferences:
  - group: ""
    kind: PersistentVolume
    jsonPointers:
      - /spec/claimRef/uid
      - /spec/claimRef/resourceVersion
      - /status
  - group: ""
    kind: PersistentVolumeClaim
    jsonPointers:
      - /metadata/annotations/volume.kubernetes.io~1storage-provisioner
      - /metadata/annotations/pv.kubernetes.io~1bind-completed
      - /metadata/annotations/pv.kubernetes.io~1bound-by-controller
      - /spec/volumeName
{{- end -}}

{{- define "platform.gitSource" -}}
source:
  repoURL: {{ .root.Values.repo.url }}
  targetRevision: {{ .root.Values.repo.revision }}
  path: platform/components/{{ .component.name }}/{{ .dir }}
  directory:
    recurse: true
{{- end -}}

{{- define "platform.labels" -}}
app.kubernetes.io/managed-by: platform-chart
homefallout.dev/component: {{ .component.name }}
homefallout.dev/tier: {{ .component.tier }}
{{- end -}}
