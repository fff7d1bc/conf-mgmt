# conf-mgmt

Configuration as a code for operating systems, built around Ansible.

## Supported operating systems

- Fedora Kinoite (Fedora Atomic)
- Bazzite (Fedora Atomic derivative)
- Ubuntu
- MacOS

## Usage

```
./runner local
./runner local NAME.yml
./runner remote NAME.yml
./runner remote NAME.yml host
./runner remote NAME.yml user@host
```

- When using `local` and not passing the name of playbook, hostname.yml will be used.
- For `remote`, if host is not provided, name of playbook without the `.yml` prefix is used as hostname. When user is not provided in target, it defaults to `root`.
