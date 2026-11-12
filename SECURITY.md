# Security

## Trust model

Every agent runs as the same OS user as you, with your rights.
`upe` is a guardrail against mistakes, not a security boundary: it cannot stop an agent that sets out to get around it.
Branch code runs in batch Editors with your rights before the council sees it.

The review behind this, what was done and what is still open: [`research/06-security.md`](research/06-security.md).

## Supported versions

The project is 0.x. Only the latest `main` gets fixes.

## Reporting a vulnerability

Report it privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability** (private vulnerability reporting). Please do not open a public issue for it.
