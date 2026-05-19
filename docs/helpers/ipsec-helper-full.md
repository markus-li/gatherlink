# IPsec Helper Full Design Notes

## Purpose

Gatherlink can carry IPsec NAT-T as UDP services.

It is not an IPsec implementation.

## Supported

- UDP/500 IKE
- UDP/4500 NAT-T

## Not directly supported

- raw ESP protocol 50
- AH protocol 51

Supporting raw ESP/AH would require generic IP tunneling or raw socket behavior,
which does not fit the core design.

## Helper role

The helper may provide:

- UDP service templates
- diagnostics
- examples for firewalls
- clear warnings about unsupported raw ESP/AH
- access_policy integration
- WireGuard/IPsec comparison notes
