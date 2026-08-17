# Grafana 13 Loki derivedFields URL field stripped during provisioning

## Symptoms

When provisioning Loki datasource with `derivedFields` containing a `url` field (e.g., `url: '$${__value.raw}'`), the Grafana API shows the `url` field as empty string `""`, even though the provisioning file has the correct value.

Example provisioning config:
```yaml
jsonData:
  derivedFields:
    - datasourceUid: tempo
      matcherRegex: '"traceId":"([a-f0-9]+)"'
      name: traceId
      url: '$${__value.raw}'
      urlDisplayLabel: 'View Trace'
```

Grafana API response:
```json
{
  "derivedFields": [
    {
      "datasourceUid": "tempo",
      "matcherRegex": "\"traceId\":\"([a-f0-9]+)\"",
      "name": "traceId",
      "url": "",
      "urlDisplayLabel": "View Trace"
    }
  ]
}
```

## Root cause

Grafana 13.1.1 (and possibly other 13.x versions) has a bug or behavior change where the `url` field in `derivedFields` is being stripped during provisioning, regardless of escaping (`$`, `$$`, or static values).

The `$$` escaping is the documented approach for YAML provisioning (to escape `$` from variable substitution), but Grafana 13 appears to be stripping the field entirely.

## Impact

- Log-trace correlation links in Grafana Explore won't work out-of-the-box
- Users must manually configure the `url` field in the Grafana UI after deployment
- The `matcherRegex` and other derivedFields properties work correctly; only `url` is affected

## Workaround

Use a sidecar container that runs alongside Grafana to patch the datasource via the Grafana API after provisioning:

```yaml
extraContainers:
  - name: fix-derived-fields
    image: curlimages/curl:latest
    command:
      - /bin/sh
      - -c
      - |
        # Wait for Grafana to be ready
        until curl -sf http://localhost:3000/api/health; do
          sleep 2
        done
        sleep 10  # Wait for provisioning to complete

        # Get admin credentials from environment
        GRAFANA_USER=$GF_SECURITY_ADMIN_USER
        GRAFANA_PASS=$GF_SECURITY_ADMIN_PASSWORD

        # Fetch current datasource
        LOKI_DS=$(curl -sf -u "$GRAFANA_USER:$GRAFANA_PASS" http://localhost:3000/api/datasources/uid/loki)

        # Patch derivedFields URLs
        PATCHED_DS=$(echo "$LOKI_DS" | jq '.jsonData.derivedFields |= map(if .name == "traceId" or .name == "trace_id" then .url = "http://tempo.tempo:3200/trace/${__value.raw}" else . end)')

        # Update datasource
        curl -sf -X PUT -u "$GRAFANA_USER:$GRAFANA_PASS" \
          -H "Content-Type: application/json" \
          -d "$PATCHED_DS" \
          http://localhost:3000/api/datasources/uid/loki

        # Keep container alive
        sleep infinity
    env:
      - name: GF_SECURITY_ADMIN_USER
        valueFrom:
          secretKeyRef:
            name: grafana-admin-secret
            key: username
      - name: GF_SECURITY_ADMIN_PASSWORD
        valueFrom:
          secretKeyRef:
            name: grafana-admin-secret
            key: password
```

**Note:** The sidecar container must have `jq` installed to parse JSON. Use `curlimages/curl` (which includes `jq`) or install it in the container.

## Related issues

- Grafana issue #63651: https://github.com/grafana/grafana/issues/63651
- Grafana issue #63657: https://github.com/grafana/grafana/issues/63657
- Similar issues reported with FluxCD, Kustomize, Helm, and Terraform doing variable substitution on `$`

## Status

**Fixed in Grafana 13.1.3.** The `url` field in `derivedFields` is now preserved during provisioning. The workaround sidecar has been removed from `system/monitoring/values.yaml`.

## Related issues

- Grafana issue #63651: https://github.com/grafana/grafana/issues/63651
- Grafana issue #63657: https://github.com/grafana/grafana/issues/63657
- Similar issues reported with FluxCD, Kustomize, Helm, and Terraform doing variable substitution on `$`
