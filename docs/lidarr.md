# Lidarr

## Change Metadata Source in SQLite DB via Ephemeral Debug Container

This guide documents how to update or revert the `metadatasource` value in the Lidarr SQLite
database using a debug container in Kubernetes (context: `grigri`).

Source: https://github.com/blampe/hearring-aid

### Steps to Change Metadata Source

1. Start a debug container with SQLite3:

   ```sh
   kubectl --context=grigri debug -n lidarr -it lidarr-0 --image=nouchka/sqlite3 --target=lidarr -- /bin/sh
   ```

   > If you don't see a command prompt, try pressing enter.

2. (Optional) Start bash for convenience:

   ```sh
   bash
   ```

3. Copy the database to `/tmp`:

   ```sh
   cp /proc/1/root/config/lidarr.db /tmp/
   ```

4. Open the database:

   ```sh
   sqlite3 /tmp/lidarr.db
   ```

5. Run the SQL to change the metadata source:

   ```sql
   INSERT INTO Config (Key, Value) VALUES ('metadatasource', 'https://api.musicinfo.pro/api/v0.4/');
   ```

6. Exit SQLite3 and copy the DB back:
   ```sh
   cp /tmp/lidarr.db /proc/1/root/config/lidarr.db
   ```

### Steps to Revert the Change

Repeat steps 1–4 above, then:

5. Run the SQL to revert:

   ```sql
   DELETE FROM Config WHERE Key = 'metadatasource';
   ```

6. Exit SQLite3 and copy the DB back:
   ```sh
   cp /tmp/lidarr.db /proc/1/root/config/lidarr.db
   ```
