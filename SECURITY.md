# Security

## Reporting

Report vulnerabilities privately through
[GitHub's security advisories](https://github.com/Shavvimal/shambu/security/advisories/new). Do not open a public
issue for a security problem. You should hear back within a week.

## What shambu touches

- **Bambu Studio's configuration** (`~/Library/Application Support/BambuStudio/BambuStudio.conf`): shambu reads the
  selected printer serial and its LAN access code from it. The code stays in memory, is never logged, printed,
  written or passed on a command line, and the serial is masked in output.
- **The printer, over LAN MQTT (TCP 8883, TLS):** shambu subscribes to the printer's report topic and publishes one
  `pushall` status request. It never starts, pauses, stops or modifies a print and never uploads files. TLS is
  verified against the CA bundle shipped with Bambu Studio; `--insecure` disables that and is the user's choice.
- **Bambu Studio's binary:** shambu runs it as a subprocess with an argument list (no shell), a timeout and a private
  data directory. Project files it reads are local files the user chose.
- **No network calls** other than the printer, and only when a `printer` command runs.

## Supported versions

The latest release on `main`. Fixes land as new releases; there are no backports.
