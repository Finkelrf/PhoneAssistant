"""HTTPS for the phone: a small private certificate authority made on first start, kept in <data>/tls.

Browsers give a page the microphone only over HTTPS (or on localhost), and Remote lives on the LAN with no public name,
so it signs its own certificate. The phone installs the authority's certificate once (GET /ca.crt), and from then on
https://<LAN address>:8797 is trusted there.

The authority is limited with X.509 name constraints to the server's own addresses (its LAN address, 127.0.0.1 and
localhost), so even if someone copied its key, a phone that trusts it would still refuse certificates for any other
site. Its key never leaves <data>/tls (mode 600). The server certificate is renewed on start when less than 30 days are
left; a new LAN address makes a new authority (the phone then installs it again).
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
LEAF_DAYS = 397          # within what browsers accept for a server certificate
RENEW_DAYS = 30


def _write(path: Path, data: bytes, mode: int):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _names(host: str):
    ips, dns = {"127.0.0.1"}, {"localhost"}
    try:
        ips.add(str(ipaddress.ip_address(host)))
    except ValueError:
        dns.add(host)
    return sorted(ips), sorted(dns)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _make_ca(ips, dns):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Remote (home network)"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Remote")])
    permitted = ([x509.IPAddress(ipaddress.ip_network(i + "/32")) for i in ips]
                 + [x509.DNSName(d) for d in dns])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _make_leaf(ca_key, ca_cert, ips, dns):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.IPAddress(ipaddress.ip_address(i)) for i in ips] + [x509.DNSName(d) for d in dns]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ips[-1] if ips else dns[0])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def ensure(data_dir: str | Path, host: str) -> dict:
    """Make or renew the certificates for `host`. Returns {'cert', 'key', 'ca'} file paths."""
    d = Path(data_dir) / "tls"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    ips, dns = _names(host)
    names = {"ips": ips, "dns": dns}
    paths = {"cert": d / "server.pem", "key": d / "server.key", "ca": d / "ca.pem", "ca_key": d / "ca.key",
             "names": d / "names.json"}

    ca_key = ca_cert = None
    try:
        if json.loads(paths["names"].read_text()) == names:
            ca_cert = x509.load_pem_x509_certificate(paths["ca"].read_bytes())
            ca_key = serialization.load_pem_private_key(paths["ca_key"].read_bytes(), None)
    except (OSError, ValueError):
        ca_key = ca_cert = None
    new_ca = ca_cert is None
    if new_ca:
        ca_key, ca_cert = _make_ca(ips, dns)
        _write(paths["ca_key"], _key_pem(ca_key), 0o600)
        _write(paths["ca"], ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
        _write(paths["names"], json.dumps(names).encode(), 0o644)

    renew = new_ca
    if not renew:
        try:
            leaf = x509.load_pem_x509_certificate(paths["cert"].read_bytes())
            left = leaf.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
            renew = left < dt.timedelta(days=RENEW_DAYS) or leaf.issuer != ca_cert.subject or not paths["key"].exists()
        except (OSError, ValueError):
            renew = True
    if renew:
        key, cert = _make_leaf(ca_key, ca_cert, ips, dns)
        _write(paths["key"], _key_pem(key), 0o600)
        _write(paths["cert"], cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return {"cert": str(paths["cert"]), "key": str(paths["key"]), "ca": str(paths["ca"])}


def ca_der(data_dir: str | Path) -> bytes | None:
    """The authority's certificate in DER, the form Android installs most easily."""
    try:
        cert = x509.load_pem_x509_certificate((Path(data_dir) / "tls" / "ca.pem").read_bytes())
    except (OSError, ValueError):
        return None
    return cert.public_bytes(serialization.Encoding.DER)
