# Nextcloud <!-- omit from toc -->

- [Migrate auth to OIDC](#migrate-auth-to-oidc)
- [Migrate Nextcloud](#migrate-nextcloud)
  - [Nextcloud](#nextcloud)
    - [apps](#apps)
    - [data](#data)
  - [Postgres](#postgres)
- [occ command](#occ-command)
- [Troubleshooting](#troubleshooting)
  - [Not automatically upgraded](#not-automatically-upgraded)
  - [Automatically upgrades fails](#automatically-upgrades-fails)

## Migrate auth to OIDC

- Configure Kanidm oauth2 with
[OpenID Connect user backend for Nextcloud](https://github.com/nextcloud/user_oidc).
- Follow Kanidm guide for
[Nextcloud config](https://github.com/kanidm/kanidm/blob/054b580fe650f012063240ba6f951c99f3c13ddc/book/src/integrations/oauth2.md#nextcloud).
- Use`displayname` from Kanidm (`name` in OIDC JWT) as user ID and keep user IDs from LDAP auth.

## Migrate Nextcloud

### Nextcloud

#### apps

```bash
POD_NAME=$(KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud get pod -l app.kubernetes.io/component=app --no-headers -o custom-columns=":metadata.name")
KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud cp config/www/nextcloud/apps/ $POD_NAME:/tmp/

KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud exec -it $POD_NAME -- bash
rm -rf /var/www/html/apps
mv /tmp/apps /var/www/html/apps
chown -R www-data:www-data /var/www/html/apps
```

#### data

```bash
POD_NAME=$(KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud get pod -l app.kubernetes.io/component=app --no-headers -o custom-columns=":metadata.name")
for DIR in $(find /datasets/nextcloud/ -maxdepth 1 -mindepth 1 -not -path '*/teresa' -not -path '*/pando' -not -path '*/billee'); do
    echo $DIR;
    KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud cp --retries=5 $DIR $POD_NAME:/var/www/html/data/
done
#KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud cp --retries=5 /datasets/fotos $POD_NAME:/var/www/html/data/pando/files/
KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud exec -it $POD_NAME -- bash
chown -R www-data:www-data /var/www/html/data
```

Alternative using rsync:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: rsync
  namespace: nextcloud
spec:
  template:
    metadata:
      name: rsync
    spec:
      containers:
        - name: rsync
          image: instrumentisto/rsync-ssh
          command:
            - rsync
            - -avz
            - --numeric-ids
            - --delete
            - --progress
            - /src/
            - /dest/data/
          volumeMounts:
            - name: src
              mountPath: "/src/"
            - name: dest
              mountPath: "/dest/"
      volumes:
        - name: src
          hostPath:
            path: /datasets/nextcloud/
            type: Directory
        - name: dest
          persistentVolumeClaim:
            claimName: nextcloud-nextcloud-data
      restartPolicy: Never
      nodeSelector:
        name: grigri
```

### Postgres

```bash
docker exec -it nextcloud_db_1 bash

PGPASSWORD="$POSTGRES_PASSWORD" pg_dump "$POSTGRES_DB" -h localhost -U "$POSTGRES_USER" -f /tmp/nextcloud-sqlbkp.bak
exit
docker cp nextcloud_db_1:/tmp/nextcloud-sqlbkp.bak /tmp/
KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud cp /tmp/nextcloud-sqlbkp.bak nextcloud-postgres-0:/tmp/

KUBECONFIG=/tmp/kubeconfig.yaml kubectl -n nextcloud exec -it nextcloud-postgres-0 -- bash
su - postgres
psql -d template1 -c "DROP DATABASE \"nextcloud\";"
psql -d template1 -c "CREATE DATABASE \"nextcloud\";"
psql -d nextcloud -f /tmp/nextcloud-sqlbkp.bak
```

## occ command

Run with kubectl:

```bash
chsh -s /bin/bash www-data
su - www-data
/var/www/html/occ
```

## Troubleshooting

### Not automatically upgraded

```bash
chsh -s /bin/bash www-data
su - www-data
/var/www/html/occ upgrade
```

### Automatically upgrades fails

```bash
chsh -s /bin/bash www-data
su - www-data
/var/www/html/occ maintenance:mode
```
