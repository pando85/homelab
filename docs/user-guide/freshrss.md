# FreshRSS

## SQLite Backup

### Export

To export data from FreshRSS using SQLite, utilize the following command:

```bash
./cli/export-sqlite-for-user.php --user ${USERNAME} --filename /tmp/freshrss.sqlite
```

### Import

To import previously exported SQLite data back into FreshRSS, use the following command:

```bash
./cli/import-sqlite-for-user.php --user ${USERNAME} --force-overwrite --filename /tmp/freshrss.sqlite
```

## PostgreSQL Configuration

Optimizing Full-text search in PostgreSQL for FreshRSS involves adding indexes. This can significantly improve search performance without modifying FreshRSS' code (which uses ILIKE).

First, ensure you have the `pg_trgm` extension installed:

```sql
CREATE EXTENSION pg_trgm;
```

Then, create the necessary indexes for title and content:

```sql
CREATE INDEX gin_trgm_index_title ON "freshrss_entry" USING gin(title gin_trgm_ops);
CREATE INDEX gin_trgm_index_content ON "freshrss_entry" USING gin(content gin_trgm_ops);
```

Replace "freshrss_entry" with the appropriate entry name (e.g., freshrss_alice_entry).

For faster searches on authors (e.g., author:Alice), add another index:

```sql
CREATE INDEX gin_trgm_index_author ON freshrss_entry USING gin(author gin_trgm_ops);
```

Repeat this process for other text fields as needed. Refer to the [CREATE TABLE _entry section](https://github.com/FreshRSS/FreshRSS/blob/edge/app/SQL/install.sql.pgsql.php) for the list of fields.

## References

- [FreshRSS Database Configuration](https://freshrss.github.io/FreshRSS/en/admins/DatabaseConfig.html)
