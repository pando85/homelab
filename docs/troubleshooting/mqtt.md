# MQTT

Connect to server mosquitto with web client:

```bash
docker run -d --name mqttx-web -p 80:80 emqx/mqttx-web
```

```yaml
name: random
client_id: random
host: mosquitto.internal.grigri.cloud
port: 8083
path: /mqtt
username: vault:mosquitto/user#username
password: vault:mosquitto/user#password
```

Subscribe to all topics with `#`.
