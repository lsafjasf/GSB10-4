#!/usr/bin/env python3
"""生成测试证书夹具与独立对拍数据（fixtures/expected.json）。

仅使用 Python 标准库；openssl 命令行仅作为"独立第三方工具"用于
生成证书和计算对照指纹（对拍依据），不参与被测库的实现。

用法: python3 gen_fixtures.py
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

CERTS = {
    # 基准证书：首次连接 / 正常重连 / 换证书对照
    "alice": dict(subj="/CN=alice.example/O=Acme", days="3650"),
    # 同主体续期证书：主体相同、密钥与序列号全新（换证书场景）
    "alice_renewed": dict(subj="/CN=alice.example/O=Acme", days="3650"),
    # 完全不同证书：主体与密钥都不同
    "bob": dict(subj="/CN=bob.example/O=Acme", days="3650"),
    # 将被吊销的证书
    "revoked": dict(subj="/CN=revoked.example/O=Acme", days="3650"),
    # 超长有效期：notAfter 落在 2050 年之后，走 GeneralizedTime 编码（边界）
    "longlived": dict(subj="/CN=longlived.example/O=Acme", days="15000"),
}


def run(*args, input_bytes=None):
    proc = subprocess.run(
        args, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "命令失败: %s\n%s" % (" ".join(args), proc.stderr.decode("utf-8", "replace"))
        )
    return proc.stdout


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gen_selfsigned(name: str, subj: str, days: str) -> None:
    run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", os.path.join(FIXTURES, name + ".key"),
        "-out", os.path.join(FIXTURES, name + ".pem"),
        "-subj", subj, "-days", days, "-batch",
    )


def gen_expired() -> None:
    """用 openssl ca 签一张 2001-2002 年有效期的证书（真实过期）。"""
    ca_dir = os.path.join(FIXTURES, "ca")
    os.makedirs(os.path.join(ca_dir, "newcerts"), exist_ok=True)
    for stale in ("index.txt", "serial"):
        path = os.path.join(ca_dir, stale)
        if os.path.exists(path):
            os.remove(path)
    open(os.path.join(ca_dir, "index.txt"), "w").close()
    with open(os.path.join(ca_dir, "serial"), "w") as fh:
        fh.write("1000\n")
    with open(os.path.join(ca_dir, "ca.cnf"), "w") as fh:
        fh.write(
            "[ca]\ndefault_ca=CA\n[CA]\ndir=%s\n"
            "database=$dir/index.txt\nnew_certs_dir=$dir/newcerts\n"
            "serial=$dir/serial\nprivate_key=$dir/cakey.pem\n"
            "certificate=$dir/cacert.pem\ndefault_md=sha256\n"
            "policy=policy_any\n[policy_any]\ncommonName=supplied\n" % ca_dir
        )
    run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", os.path.join(ca_dir, "cakey.pem"),
        "-out", os.path.join(ca_dir, "cacert.pem"),
        "-subj", "/CN=fixture-ca", "-days", "3650", "-batch",
    )
    run(
        "openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
        "-keyout", os.path.join(FIXTURES, "expired.key"),
        "-out", os.path.join(ca_dir, "expired.csr"),
        "-subj", "/CN=expired.example/O=Acme", "-batch",
    )
    run(
        "openssl", "ca", "-batch", "-config", os.path.join(ca_dir, "ca.cnf"),
        "-startdate", "20010101000000Z", "-enddate", "20020101000000Z",
        "-in", os.path.join(ca_dir, "expired.csr"),
        "-out", os.path.join(FIXTURES, "expired.pem"),
    )
    # openssl ca 的输出带可读文本头，重编码为纯 PEM
    clean = run(
        "openssl", "x509", "-in", os.path.join(FIXTURES, "expired.pem"),
        "-outform", "PEM",
    )
    with open(os.path.join(FIXTURES, "expired.pem"), "wb") as fh:
        fh.write(clean)


def openssl_expectations(pem_path: str) -> dict:
    """用 openssl 独立计算对拍数据：三枚指纹 + 序列号 + 有效期。"""
    der = run("openssl", "x509", "-in", pem_path, "-outform", "DER")
    cert_fp = sha256_hex(der)

    pub_pem = run("openssl", "x509", "-in", pem_path, "-pubkey", "-noout")
    spki_der = run("openssl", "pkey", "-pubin", "-outform", "DER", input_bytes=pub_pem)
    spki_fp = sha256_hex(spki_der)

    # 用 asn1parse 定位 tbsCertificate 内 d=2 的 SEQUENCE：
    # 顺序固定为 sigAlg, issuer, validity, subject, SPKI
    dump = run("openssl", "asn1parse", "-in", pem_path, "-inform", "PEM").decode()
    offsets = [
        int(m.group(1))
        for m in re.finditer(r"^\s*(\d+):d=2\s+hl=\d+\s+l=\s*\d+\s+cons:\s+SEQUENCE", dump, re.M)
    ]
    if len(offsets) < 5:
        raise RuntimeError("asn1parse 输出不符合预期: %s" % pem_path)
    subject_der = run(
        "openssl", "asn1parse", "-in", pem_path, "-inform", "PEM",
        "-strparse", str(offsets[3]), "-out", "/dev/stdout", "-noout",
    )
    spki_der2 = run(
        "openssl", "asn1parse", "-in", pem_path, "-inform", "PEM",
        "-strparse", str(offsets[4]), "-out", "/dev/stdout", "-noout",
    )
    subject_fp = sha256_hex(subject_der)
    if sha256_hex(spki_der2) != spki_fp:
        raise RuntimeError("openssl 两条路径计算的 SPKI 指纹不一致: %s" % pem_path)

    serial_txt = run("openssl", "x509", "-in", pem_path, "-noout", "-serial").decode().strip()
    serial = int(serial_txt.split("=", 1)[1], 16)

    dates = run("openssl", "x509", "-in", pem_path, "-noout", "-dates").decode()

    def parse_date(line: str) -> str:
        raw = line.split("=", 1)[1].strip()
        dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return dt.isoformat()

    lines = dict(ln.split("=", 1) for ln in dates.strip().splitlines())
    return {
        "cert_fp": cert_fp,
        "spki_fp": spki_fp,
        "subject_fp": subject_fp,
        "serial": serial,
        "not_before": parse_date("notBefore=" + lines["notBefore"]),
        "not_after": parse_date("notAfter=" + lines["notAfter"]),
    }


def main() -> None:
    os.makedirs(FIXTURES, exist_ok=True)
    for name, cfg in CERTS.items():
        gen_selfsigned(name, cfg["subj"], cfg["days"])
        print("生成证书 %s" % name)
    gen_expired()
    print("生成证书 expired（2001-2002 有效期）")

    expected = {}
    for name in list(CERTS) + ["expired"]:
        expected[name] = openssl_expectations(os.path.join(FIXTURES, name + ".pem"))
    with open(os.path.join(FIXTURES, "expected.json"), "w") as fh:
        json.dump(expected, fh, indent=2, ensure_ascii=False)
    print("对拍数据已写入 fixtures/expected.json")


if __name__ == "__main__":
    sys.exit(main())
