import unittest
from types import SimpleNamespace

from app.api.routes.access import client_address


def req(peer, headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers or {})


class ClientIpTests(unittest.TestCase):
    def test_real_ip_only_from_our_proxies(self):
        self.assertEqual(client_address(req("172.18.0.5", {"x-real-ip": "95.31.10.2"})), "95.31.10.2")
        self.assertEqual(client_address(req("172.18.0.5")), "172.18.0.5")
        self.assertEqual(client_address(req("172.18.0.5", {"x-real-ip": "junk"})), "172.18.0.5")
        # straight from the internet: a forged header is ignored
        self.assertEqual(client_address(req("95.31.10.2", {"x-real-ip": "1.2.3.4"})), "95.31.10.2")


if __name__ == "__main__":
    unittest.main()
