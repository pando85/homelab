# Valetudo upgrade

## Operation

```bash
ssh root@tanque.iot.grigri
killall valetudo
wget https://github.com/Hypfer/Valetudo/releases/latest/download/valetudo-aarch64 -O /data/valetudo
reboot
```

## Troubleshooting

### "Text file busy" error

Valetudo is still running. Try to kill it again.

If the issue still occurs, delete the old binary before uploading the new one.
