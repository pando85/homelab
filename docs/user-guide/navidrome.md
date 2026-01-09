# Navidrome

## Update music metadata

```bash
# Explore pod
kubectl --context=grigri -n navidrome exec -it $(kubectl -n navidrome get pod -l app.kubernetes.io/name=navidrome -o jsonpath="{.items[0].metadata.name}") -- /bin/sh

# Copy files to local
kubectl --context=grigri -n navidrome cp $(kubectl -n navidrome get pod -l app.kubernetes.io/name=navidrome -o jsonpath="{.items[0].metadata.name}"):/music/path/ /tmp/path/

# Change metadata with local tools
easytag /tmp/path/

# Copy files back to pod
kubectl --context=grigri -n navidrome cp /tmp/path/ $(kubectl -n navidrome get pod -l app.kubernetes.io/name=navidrome -o jsonpath="{.items[0].metadata.name}"):/music/path/
```

After updating the metadata, you may need to rescan the music library from the Navidrome web
interface and remove missing files:
[https://navidrome.grigri.cloud/app/#/missing](https://navidrome.grigri.cloud/app/#/missing).
