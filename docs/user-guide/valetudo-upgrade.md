# Valetudo upgrade

## Valetudo

```bash
ssh root@tanque.iot.grigri
killall valetudo
wget https://github.com/Hypfer/Valetudo/releases/latest/download/valetudo-aarch64 -O /data/valetudo
reboot
```

## Firmware

**Note:** Dreame doesn't provide any firmware update changelog or information about the new firmware.

Check if there are new firmware versions available on the [Dreame L10S Ultra Dustbuilder](https://builder.dontvacuum.me/_dreame_r2228.html).

### Backup

```bash
ssh root@tanque.iot.grigri
dd if=/dev/by-name/private | gzip -9 > /tmp/backup_private.dd.gz
dd if=/dev/by-name/misc | gzip -9 > /tmp/backup_misc.dd.gz
tar -czvf /tmp/backup_mnt.tar.gz /mnt
exit
scp -r -O root@tanque.iot.grigri:/tmp/backup_private.dd.gz .
scp -r -O root@tanque.iot.grigri:/tmp/backup_misc.dd.gz .
scp -r -O root@tanque.iot.grigri:/tmp/backup_mnt.tar.gz .
```

### Build firmware

Get the firmware from [the dustbuilder](https://builder.dontvacuum.me/) and download it to the robot.

Go to [Dreame L10S Ultra](https://builder.dontvacuum.me/_dreame_r2228.html) section.

Email: `anonymous`

Authorized_keys: `pass personal/authorized_keys`

**Note:** Just the first line is used.

Get serial number and config value from `pass iot/tanque_firmware`.

**Note:** config value from fastboot doesn't change.

### Install

You need to have your robot already rooted to use this firmware!
The robot needs to be in its docking station and fully charged!

0. Connect to robot via SSH using your SSH key

   ```bash
   ssh root@tanque.iot.grigri
   ```

1. Download firmware update to `/tmp`

   ```bash
   cd /tmp
   wget --no-check-certificate {url-of-firmware.tar.gz}
   ```

2. Unpack firmware package

   ```bash
   tar -xzvf {name-of-firmware.tar.gz}
   ```

3. Run installer

   ```bash
   ./install.sh
   ```

The robot should install the firmware and reboot. This steps will update the Kernel, Rootfs and MCU firmware

**Note:** It takes like 3 minutes to install and reboot since `{"ret":"ok"}` is shown.

## Troubleshooting

### "Text file busy" error

Valetudo is still running. Try to kill it again.

If the issue still occurs, delete the old binary before uploading the new one.
