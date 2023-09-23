# Restic

## Troubleshooting

### Unable to Create Lock in Restic Backend

If you encounter the following error message in your Restic logs:

```
Fatal: unable to create lock in backend: repository is already locked by...
```

To resolve this issue:

- Access the `volsync-src-XXX-backup` pod where the issue is occurring.

- Inside the pod, open a terminal.

- Run the following command to remove the lock:

```bash
restic unlock --remove-all
```

This should resolve the lock issue in the Restic repository.
