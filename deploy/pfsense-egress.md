# pfSense Egress Notes

For the poll-only v1, the container only needs outbound HTTPS to:

- `api.fastmail.com:443`

If JMAP push is enabled later, allow the host returned by the Fastmail JMAP
session `eventSourceUrl`, expected to be under Fastmail-controlled domains.
