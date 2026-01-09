# Navidrome

## Update music metadata

```bash
# Explore pod
kubectl --context=grigri -n navidrome exec -it $(kubectl -n navidrome get pod -l app.kubernetes.io/name=navidrome -o jsonpath="{.items[0].metadata.name}") -- /bin/sh

# Copy files to local
kubectl --context=grigri -n navidrome cp $(kubectl -n navidrome get pod -l app.kubernetes.io/name=navidrome -o jsonpath="{.items[0].metadata.name}"):/music/path/ /tmp/path/
```
