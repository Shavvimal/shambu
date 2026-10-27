# Security

## Reporting

Report vulnerabilities privately through
[GitHub's security advisories](https://github.com/Shavvimal/shambu/security/advisories/new). Do not open a public
issue for a security problem. You should hear back within a week.

## What shambu touches

- **Bambu Studio's configuration** (`~/Library/Application Support/BambuStudio/BambuStudio.conf`): shambu reads the
  selected printer serial and its LAN access code from it, and never writes it. Studio's stored code is the last
  source of the code: a one-shot flag (`--access-code-file`, `--access-code-stdin`) comes first, then the code that
  `shambu printer login` stored for the profile's serial, then Studio's. The code stays in memory, is never logged,
  printed or passed on a command line, and the serial is masked in output.
- **shambu's config dir** (`$SHAMBU_CONFIG_DIR`, else `$XDG_CONFIG_HOME/shambu`, else `~/.config/shambu`; a relative
  or empty value counts as unset). `printers.json` holds the printer profiles: names, serials, IPs and modes, never a
  code. `credentials.json` holds the access codes, mode 0600; shambu creates the dir 0700. shambu refuses a symlink
  or anything that is not a regular file at either path. `shambu printer forget --printer NAME` removes the profile
  and its code. The code file gets the same protection as Studio's own plain-text config against another process
  that runs as the same user, with a tighter file mode. It lives outside any repository.
- **The printer, over LAN MQTT (TCP 8883, TLS):** shambu subscribes to the printer's report topic and publishes one
  `pushall` status request. It never starts, pauses, stops or modifies a print and never uploads files. TLS is
  verified against the CA bundle shipped with Bambu Studio; `--insecure` disables that and is the user's choice.
- **Bambu Studio's binary:** shambu runs it as a subprocess with an argument list (no shell), a timeout and a private
  data directory. Project files it reads are local files the user chose.
- **The network, and only from the commands that read the printer** (`doctor`, `check`, `build`, `sweep` and every
  `printer` action). shambu connects to the printer's broker on TCP 8883. A read through a profile goes to the IP the
  profile stores and does not search. To find the printer it connects to TCP 8883 on candidate hosts: the hosts in the
  ARP table, as 0.4.0 does, and then, in a subnet pass, the other hosts of the IPv4 network of the interface that
  carries the default route, only when its mask is /24 or narrower. The subnet pass runs in `printer discover`
  (always), in `login` when it has no IP (`--from-studio` without `--ip`), and on a profile read only when the profile
  has no IP, which `login` never leaves. A read without a profile and without `--ip` uses the ARP table only. A wider
  mask scans nothing. A point-to-point interface (`inet A --> B`, for example `utun`) is not scanned. A candidate gets
  a TCP connect and, when the serial came from a flag or a profile and TLS is verified, a TLS handshake that reads its
  certificate, and nothing else; a serial from Studio's config gets a TCP connect only. With TLS verified, no access
  code goes to a host before its certificate names the serial. There are no other network calls.

First-time enrolment has a trust boundary. `shambu printer login --ip IP` without `--serial` takes the serial from the
certificate that answers at that IP, so the profile binds to whatever Bambu-signed device answers there: a
chain-valid certificate proves a Bambu device, not the intended one. The serial from the printer's touchscreen,
given as `--serial`, is the stronger binding.

## Supported versions

The latest release on `main`. Fixes land as new releases; there are no backports.
