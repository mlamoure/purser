{{- define "purser.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "purser.selectorLabels" -}}
app.kubernetes.io/name: purser
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "purser.labels" -}}
{{ include "purser.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{- end -}}

{{- define "purser.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "purser.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{- define "purser.secretName" -}}
{{- if .Values.existingSecret -}}
{{ .Values.existingSecret }}
{{- else -}}
{{ include "purser.fullname" . }}-secrets
{{- end -}}
{{- end -}}

{{/*
Image reference. The tag defaults to the release tag the container workflow
publishes (v + appVersion); a digest, when set, pins the exact image.
*/}}
{{- define "purser.image" -}}
{{- $tag := .Values.image.tag | default (printf "v%s" .Chart.AppVersion) -}}
{{- if .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" .Values.image.digest) -}}
{{- fail "image.digest must look like sha256:<64 hex characters>" -}}
{{- end -}}
{{ printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest }}
{{- else -}}
{{ printf "%s:%s" .Values.image.repository $tag }}
{{- end -}}
{{- end -}}

{{/*
Render-time guards. Every template that needs the configuration valid
includes this, so `helm template` and `helm install` refuse a bad release
before anything reaches the cluster.
*/}}
{{- define "purser.validate" -}}
{{- if not (has .Values.postgres.mode (list "external" "bundled")) -}}
{{- fail (printf "postgres.mode must be \"external\" or \"bundled\", not %q" .Values.postgres.mode) -}}
{{- end -}}
{{- /* Exactly one scheduler (DESIGN section 2): it runs in the worker when the
       worker is enabled, otherwise in the API process. */ -}}
{{- if .Values.worker.enabled -}}
{{- if ne (int .Values.worker.replicas) 1 -}}
{{- fail "worker.replicas must be 1: the worker runs the scheduler, and two workers would run every job twice" -}}
{{- end -}}
{{- else if ne (int .Values.api.replicas) 1 -}}
{{- fail "api.replicas must be 1 while worker.enabled=false: each API pod would run its own scheduler. Enable the worker to scale the API" -}}
{{- end -}}
{{- /* Secrets never travel as plain-text env values. */ -}}
{{- range $k, $v := (.Values.env | default dict) -}}
{{- if regexMatch "(?i)(SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|ENCRYPTION_KEY|DATABASE_URL)" $k -}}
{{- fail (printf "env.%s looks like a secret: put it in secrets.extra (or your existingSecret), not in env, which renders as plain text in the pod spec" $k) -}}
{{- end -}}
{{- end -}}
{{- range $k, $v := (.Values.secrets.extra | default dict) -}}
{{- if not (regexMatch "^[A-Za-z_][A-Za-z0-9_]*$" $k) -}}
{{- fail (printf "secrets.extra key %q is not a valid environment variable name" $k) -}}
{{- end -}}
{{- end -}}
{{- if eq .Values.postgres.mode "bundled" -}}
{{- if and (not .Values.postgres.existingSecret) (not .Values.postgres.password) -}}
{{- fail "postgres.mode=bundled needs postgres.password or postgres.existingSecret. Generate one with `openssl rand -hex 24`" -}}
{{- end -}}
{{- if and .Values.postgres.password (not (regexMatch "^[A-Za-z0-9._~-]{16,}$" .Values.postgres.password)) -}}
{{- fail "postgres.password must be at least 16 characters of A-Z a-z 0-9 . _ ~ - (it is embedded in the database URL unescaped); `openssl rand -hex 24` gives one" -}}
{{- end -}}
{{- end -}}
{{- if not .Values.existingSecret -}}
{{- if not .Values.secrets.encryptionKey -}}
{{- fail "secrets.encryptionKey is required (or set existingSecret to a Secret with a PURSER_ENCRYPTION_KEY key). Generate it with `purser keygen` and RECORD IT: encrypted data is unrecoverable without it" -}}
{{- end -}}
{{- if and (not .Values.secrets.databaseUrl) (not (and (eq .Values.postgres.mode "bundled") .Values.postgres.password)) -}}
{{- fail "secrets.databaseUrl is required (or set existingSecret to a Secret with a PURSER_DATABASE_URL key). With postgres.mode=bundled the chart derives it from postgres.password" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "purser.databaseUrl" -}}
{{- if .Values.secrets.databaseUrl -}}
{{ .Values.secrets.databaseUrl }}
{{- else -}}
postgresql+psycopg://{{ .Values.postgres.username }}:{{ .Values.postgres.password }}@{{ include "purser.fullname" . }}-postgres:5432/{{ .Values.postgres.database }}
{{- end -}}
{{- end -}}

{{/*
Container env shared by the api and worker Deployments: every .Values.env
entry, plus PURSER_PUBLIC_URL derived from the ingress host when env does not
set it. It drives secure cookies, the OIDC redirect URI and the MCP endpoint's
Host/Origin allowlist, which answers 421 to a host it does not list.
*/}}
{{- define "purser.env" -}}
{{- $env := .Values.env | default dict -}}
{{- range $k, $v := $env }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- if and .Values.ingress.enabled .Values.ingress.host (not (hasKey $env "PURSER_PUBLIC_URL")) }}
- name: PURSER_PUBLIC_URL
  value: {{ printf "https://%s" .Values.ingress.host | quote }}
{{- end }}
{{- /* The root filesystem is read-only; skip pointless bytecode writes. */}}
{{- if not (hasKey $env "PYTHONDONTWRITEBYTECODE") }}
- name: PYTHONDONTWRITEBYTECODE
  value: "1"
{{- end }}
{{- end -}}

{{/*
Pod-level fields shared by the api and worker Deployments.
*/}}
{{- define "purser.podSpecCommon" -}}
serviceAccountName: {{ include "purser.serviceAccountName" . }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountToken }}
enableServiceLinks: false
{{- with .Values.imagePullSecrets }}
imagePullSecrets: {{- toYaml . | nindent 2 }}
{{- end }}
securityContext: {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.nodeSelector }}
nodeSelector: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations: {{- toYaml . | nindent 2 }}
{{- end }}
volumes:
  - name: tmp
    emptyDir:
      sizeLimit: {{ .Values.tmpSizeLimit }}
{{- end -}}
