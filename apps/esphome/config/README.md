# Esphome devices

## Local development

```bash
pip install -r requirements.txt
kubectl -n esphome get secret esphome-secrets -o jsonpath='{ .data.secrets\.yaml }' | base64 -d > secrets.yaml

esphome run rack-controller.yaml
```
