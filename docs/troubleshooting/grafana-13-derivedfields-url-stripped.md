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

After the datasource is provisioned, manually edit the Loki datasource in the Grafana UI:
1. Go to Configuration → Data Sources → Loki
2. In the "Derived fields" section, edit the traceId field
3. Set the "URL/query" field to `${__value.raw}`
4. Save the datasource

## Related issues

- Grafana issue #63651: https://github.com/grafana/grafana/issues/63651
- Grafana issue #63657: https://github.com/grafana/grafana/issues/63657
- Similar issues reported with FluxCD, Kustomize, Helm, and Terraform doing variable substitution on `$`

## Status

Configuration is committed with the documented `$$` escaping. The `url` field will be empty until manually configured in the Grafana UI or until Grafana fixes the provisioning bug.
