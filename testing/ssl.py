"""Create self-signed SSL certificates for testing.

Warning:
    None of the functions in this module are safe and should only be used
    for creating temporary self-signed certificates for testing.

Note:
    Based on the examples in the
    [Cryptography docs](https://cryptography.io/en/latest/x509/tutorial/#creating-a-self-signed-certificate).
"""

from __future__ import annotations

import datetime
import pathlib
import ssl
from typing import NamedTuple

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.x509 import Certificate
from cryptography.x509.oid import NameOID


class SSLContextFixture(NamedTuple):
    """SSL fixture return type."""

    certfile: str
    keyfile: str
    ssl_context: ssl.SSLContext


@pytest.fixture(scope='session')
def ssl_context(tmp_path_factory: pytest.TempPathFactory) -> SSLContextFixture:
    """Create an SSL context from a self-signed certificate."""
    tmp_path = tmp_path_factory.mktemp('ssl-context-fixture')
    certfile = tmp_path / 'cert.pem'
    keyfile = tmp_path / 'key.pem'
    cert, key = create_self_signed_cert()
    write_cert_key_pair(cert, key, certfile, keyfile)

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile, keyfile=keyfile)

    return SSLContextFixture(str(certfile), str(keyfile), ssl_context)


def create_self_signed_cert() -> tuple[Certificate, RSAPrivateKey]:
    """Creates a self-signed certificate and private key pair."""
    key = generate_private_key(public_exponent=65537, key_size=2048)

    # Various details about who we are. For a self-signed certificate the
    # subject and issuer are always the same.
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, 'US'),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, 'Illinois'),
            x509.NameAttribute(NameOID.LOCALITY_NAME, 'Chicago'),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Globus Labs'),
            x509.NameAttribute(NameOID.COMMON_NAME, 'labs.globus.com'),
        ],
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            # Our certificate will be valid for 10 days
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=10),
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName('localhost')]),
            critical=False,
            # Sign our certificate with our private key
        )
        .sign(key, hashes.SHA256())
    )

    return cert, key


def write_cert_key_pair(
    cert: Certificate,
    key: RSAPrivateKey,
    certfile: str | pathlib.Path,
    keyfile: str | pathlib.Path,
) -> None:
    """Write a self-signed certificate and private key file to disk."""
    encrypted_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(keyfile, 'wb') as f:
        f.write(encrypted_key)

    with open(certfile, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
