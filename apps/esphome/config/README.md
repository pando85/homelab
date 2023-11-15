# Esphome devices

## Local development

```bash
pip install -r requirements.txt
kubectl -n esphome get secret esphome-secrets -o jsonpath='{ .data.secrets\.yaml }' | base64 -d > secrets.yaml

esphome run rack-controller.yaml
```

For dashboard:
```bash
docker run --rm -v "${PWD}":/config -p 6052:6052 -e ESPHOME_DASHBOARD_USE_PING=true -it ghcr.io/esphome/esphome
```
