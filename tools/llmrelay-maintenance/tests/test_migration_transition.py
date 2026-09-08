"""Protect the narrow reviewed upgrade path and refuse unreviewed schemas."""
import unittest
import deploy


class MigrationTransitionTests(unittest.TestCase):
    def setUp(self):
        self.current = {"revision": "aa236488351eb71e120fc2b6fb32e36b0374c918",
                        "migrations_sha256": "cfd6e44c66258419248741d14d9981fc734a314cab00ee95c44547f7abceef1e"}
        self.target = {"revision": "578785ee7fb35030b094b69624efe25670a36f5f",
                       "migrations_sha256": "c457086f49728b5c59eb875ccacbc57b17bf4fdcaef3288288ee2fbd5bb074ef"}

    def test_exact_reviewed_transition_is_identified(self):
        self.assertEqual(deploy.Deployment.compatible(self.current, self.target), "0.2.0-to-0.2.1-additive")

    def test_changing_any_reviewed_revision_or_schema_refuses_upgrade(self):
        for side in ("current", "target"):
            for key, size in (("revision", 40), ("migrations_sha256", 64)):
                with self.subTest(side=side, field=key):
                    current, target = dict(self.current), dict(self.target)
                    (current if side == "current" else target)[key] = "0" * size
                    with self.assertRaises(deploy.DeploymentError):
                        deploy.Deployment.compatible(current, target)

    def test_reverse_direction_is_not_a_general_downgrade_permission(self):
        # Application rollback uses the original, verified transaction direction.
        with self.assertRaises(deploy.DeploymentError):
            deploy.Deployment.compatible(self.target, self.current)

    def test_identical_schema_retains_existing_behavior(self):
        target = dict(self.current, revision="f" * 40)
        self.assertEqual(deploy.Deployment.compatible(self.current, target), "identical")


class AllowlistRepairTransitionTests(MigrationTransitionTests):
    def setUp(self):
        self.current = {"revision": "5485f368b29d05adb95a00f71801c7c23d8f48af",
                        "migrations_sha256": "e5e58e07acb1018cbd3a2427054eb453263d84e8b773aca907c2c73051e2a96f"}
        self.target = {"revision": "8fa67d477d6651a744754392a8982ea589c26ae6",
                       "migrations_sha256": "37008c238c0b9214fe5d414a08b023263d6694b899d704fe1c49f21a619f3bd6"}

    def test_exact_reviewed_transition_is_identified(self):
        self.assertEqual(deploy.Deployment.compatible(self.current, self.target),
                         "0.2.2-to-0.2.3-allowlist-repair")


if __name__ == "__main__":
    unittest.main()
