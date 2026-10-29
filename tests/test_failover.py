"""Deterministic failover and durable fallback-store tests (temporary SQLite only)."""

import os
import tempfile
import unittest

from backend.failover import (FailoverController, FailoverError, FailoverState,
                              FallbackStore, FallbackSynchronizer, ForwardResult,
                              Heartbeat, HeartbeatMonitor, sign_ownership_assertion)
import hashlib


class FakeClock:
    def __init__(self, value=1000.0): self.value = value
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "fallback.sqlite")
        self.store = FallbackStore(self.path)
        self.clock = FakeClock()
        self.key = b"unit-test-only-key"
        self.controller = FailoverController(self.store, "pi-primary", self.key,
            interval_s=5, timeout_s=25, activation_s=2, target_s=90,
            clock=self.clock, wall_clock=self.clock)

    def tearDown(self): self.tmp.cleanup()

    def fail_primary(self):
        self.controller.set_simulation(primary_running=False, drop_heartbeats=True)
        self.clock.advance(25)
        self.controller.tick()

    def activate(self):
        self.fail_primary()
        self.clock.advance(2)
        self.controller.tick()

    def report(self, incident_id="INC-fallback-1"):
        return {"incident_id": incident_id, "alert_type": "medical",
                "description": "assistance", "latitude": None, "longitude": None}

    def test_primary_heartbeat_keeps_backup_standby(self):
        self.assertEqual(self.controller.state, FailoverState.BACKUP_STANDBY)
        self.assertTrue(self.controller.monitor.fresh)
        self.assertFalse(self.controller.fallback_service_ready)

    def test_stale_heartbeat_requires_activation_then_active(self):
        self.fail_primary()
        self.assertEqual(self.controller.state, FailoverState.FALLBACK_ACTIVATING)
        self.assertFalse(self.controller.fallback_service_ready)
        self.clock.advance(2)
        self.controller.tick()
        self.assertEqual(self.controller.state, FailoverState.FALLBACK_ACTIVE)
        self.assertTrue(self.controller.fallback_service_ready)

    def test_simulated_failover_is_under_ninety_seconds(self):
        start = self.controller.monitor.last_received_at
        self.activate()
        elapsed = self.controller.clock() - start
        self.assertEqual(elapsed, 27)
        self.assertLess(elapsed, 90)

    def test_malformed_duplicate_out_of_order_stale_and_bad_mac_rejected(self):
        monitor = self.controller.monitor
        self.assertFalse(monitor.receive({"sequence": "bad"}))
        duplicate = Heartbeat.signed("pi-primary", "pi-primary", 0, self.clock(), self.key)
        self.assertFalse(monitor.receive(duplicate))
        old = Heartbeat.signed("pi-primary", "pi-primary", 1, self.clock()-40, self.key)
        self.assertFalse(monitor.receive(old))
        forged = Heartbeat.signed("pi-primary", "pi-primary", 1, self.clock(), b"wrong")
        self.assertFalse(monitor.receive(forged))

    def test_partition_with_live_primary_degrades_without_promotion(self):
        self.controller.set_simulation(link_available=False)
        self.clock.advance(25)
        self.controller.tick()
        self.assertEqual(self.controller.state, FailoverState.DEGRADED)
        self.assertEqual(self.controller.node_role.value, "backup")
        self.assertFalse(self.controller.fallback_service_ready)
        self.assertIn("ambiguous", self.controller.reason)

    def test_invalid_transition_rejected(self):
        with self.assertRaises(FailoverError):
            self.controller.transition(FailoverState.FALLBACK_ACTIVE, "skip activation")

    def test_fallback_report_is_durable_before_local_acceptance(self):
        self.activate()
        accepted, created = self.store.accept(self.report(), "esp32-sim", self.clock())
        self.assertTrue(created)
        self.assertEqual(accepted["local_receipt_id"], "LOCAL-INC-fallback-1")
        self.assertEqual(accepted["outbox_state"], "pending")
        self.assertEqual(self.store.pending_count(), 1)

    def test_duplicate_fallback_submission_preserves_identity(self):
        first, _ = self.store.accept(self.report(), "esp32-sim", self.clock())
        again, created = self.store.accept(self.report(), "esp32-sim", self.clock())
        self.assertFalse(created)
        self.assertEqual(first["incident_id"], again["incident_id"])
        self.assertEqual(self.store.pending_count(), 1)

    def test_preprovisioned_ownership_assertion_survives_store_restart_and_conflicts_fail_closed(self):
        identity="12345678-1234-4234-8234-123456789abc"
        conflicting="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.store.provision_citizen(identity);self.store.provision_citizen(conflicting)
        assertion=sign_ownership_assertion(self.key,"INC-owner-restart",identity,
            "esp32-sim","pi-primary","esp32-sim")
        report={**self.report("INC-owner-restart"),"citizen_user_id":7,
            "owner_identity_id":identity,"ownership_state":"verified",
            "ownership_issuer":"esp32-sim","ownership_assertion":assertion,
            "ownership_assertion_digest":hashlib.sha256(assertion.encode()).hexdigest()}
        self.store.accept(report,"esp32-sim",self.clock())
        restarted=FallbackStore(self.path)
        recovered=restarted.pending()[0]
        self.assertEqual(recovered["owner_identity_id"],identity)
        self.assertEqual(recovered["ownership_state"],"verified")
        conflict_assertion=sign_ownership_assertion(self.key,"INC-owner-restart",conflicting,
            "esp32-sim","pi-primary","esp32-sim")
        conflicting_report={**report,"owner_identity_id":conflicting,
            "ownership_assertion":conflict_assertion,
            "ownership_assertion_digest":hashlib.sha256(conflict_assertion.encode()).hexdigest()}
        with self.assertRaises(FailoverError):
            restarted.accept(conflicting_report,"esp32-sim",self.clock())
        self.assertEqual(restarted.pending()[0]["owner_identity_id"],identity)

    def test_ownership_assertion_is_bound_to_incident_issuer_audience_and_node(self):
        from backend.failover import verify_ownership_assertion
        tag=sign_ownership_assertion(self.key,"INC-bound","12345678-1234-4234-8234-123456789abc",
            "backup-a","primary-a","backup-a")
        args=(self.key,tag,"INC-bound","12345678-1234-4234-8234-123456789abc")
        self.assertTrue(verify_ownership_assertion(*args,"backup-a","primary-a","backup-a"))
        self.assertFalse(verify_ownership_assertion(*args,"backup-a","primary-a","backup-b"))
        self.assertFalse(verify_ownership_assertion(self.key,tag,"INC-other",args[3],"backup-a","primary-a","backup-a"))
        self.assertFalse(verify_ownership_assertion(*args,"backup-b","primary-a","backup-a"))
        self.assertFalse(verify_ownership_assertion(*args,"backup-a","primary-b","backup-a"))
        commitment="a"*64
        bound=sign_ownership_assertion(self.key,"INC-bound",args[3],"backup-a","primary-a","backup-a",commitment)
        self.assertTrue(verify_ownership_assertion(self.key,bound,"INC-bound",args[3],"backup-a","primary-a","backup-a",commitment))
        self.assertFalse(verify_ownership_assertion(self.key,bound,"INC-bound",args[3],"backup-a","primary-a","backup-a","b"*64))

    def test_fallback_rows_and_runtime_survive_restart(self):
        self.activate()
        self.store.accept(self.report(), "esp32-sim", self.clock())
        restarted = FallbackStore(self.path)
        self.assertEqual(restarted.pending_count(), 1)
        self.assertEqual(restarted.pending()[0]["incident_id"], "INC-fallback-1")
        self.assertEqual(restarted.get_runtime()["state"], FailoverState.FALLBACK_ACTIVE.value)
        rebuilt = FailoverController(restarted, "pi-primary", self.key,
            interval_s=5, timeout_s=25, activation_s=2, target_s=90,
            clock=self.clock, wall_clock=self.clock)
        self.assertTrue(rebuilt.fallback_service_ready)

    def test_backup_restart_does_not_trust_previous_process_heartbeat_age(self):
        rebuilt = FailoverController(FallbackStore(self.path), "pi-primary", self.key,
            interval_s=5, timeout_s=25, activation_s=2, target_s=90,
            clock=FakeClock(10), wall_clock=FakeClock(2000))
        self.assertEqual(rebuilt.state, FailoverState.DEGRADED)
        self.assertFalse(rebuilt.monitor.fresh)

    def test_failed_forwarding_retains_pending_record(self):
        self.activate()
        self.store.accept(self.report(), "esp32-sim", self.clock())
        sync = FallbackSynchronizer(self.store,
            lambda row: ForwardResult(False, detail="offline"), self.clock)
        result = sync.sync_once()
        self.assertEqual(result["pending"], 1)
        self.assertEqual(self.store.pending()[0]["attempts"], 1)

    def test_local_receipt_cannot_be_marked_command_centre_receipt(self):
        self.store.accept(self.report(), "esp32-sim", self.clock())
        with self.assertRaises(FailoverError):
            self.store.mark_received("INC-fallback-1", "LOCAL-INC-fallback-1")

    def test_successful_sync_clears_only_after_destination_receipt(self):
        self.store.accept(self.report(), "esp32-sim", self.clock())
        sync = FallbackSynchronizer(self.store,
            lambda row: ForwardResult(True, "RCP-persisted"), self.clock)
        result = sync.sync_once()
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["received"], 1)

    def test_recovery_requires_fresh_primary_and_primary_readiness(self):
        self.activate()
        self.controller.set_simulation(primary_running=True, primary_ready=False, drop_heartbeats=False)
        self.controller.tick()
        self.assertEqual(self.controller.state, FailoverState.PRIMARY_RECOVERING)
        self.controller.tick()
        self.assertEqual(self.controller.state, FailoverState.PRIMARY_RECOVERING)
        self.controller.set_simulation(primary_ready=True)
        self.controller.tick()
        self.assertEqual(self.controller.state, FailoverState.SYNCING_INCIDENTS)

    def test_partitioned_recovery_does_not_discard_outbox(self):
        self.activate()
        self.store.accept(self.report(), "esp32-sim", self.clock())
        self.controller.set_simulation(primary_running=True, link_available=False, drop_heartbeats=False)
        self.controller.transition(FailoverState.PRIMARY_RECOVERING, "simulated primary recovery began")
        self.clock.advance(25)
        self.controller.tick()
        self.assertEqual(self.controller.state, FailoverState.DEGRADED)
        self.assertEqual(self.store.pending_count(), 1)

    def test_pending_records_not_duplicated_by_retried_sync(self):
        self.store.accept(self.report(), "esp32-sim", self.clock())
        count = {"dest": 0}
        def idempotent_destination(row):
            count["dest"] += 1
            return ForwardResult(True, "RCP-stable")
        sync = FallbackSynchronizer(self.store, idempotent_destination, self.clock)
        sync.sync_once(); sync.sync_once()
        self.assertEqual(count["dest"], 1)
        self.assertEqual(self.store.pending_count(), 0)

    def test_fallback_store_is_confined_to_its_temporary_database(self):
        production = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "database", "nexalert.db"))
        self.assertNotEqual(os.path.realpath(self.store.path), production)
        self.store.accept(self.report("INC-temp-only"), "esp32-sim", self.clock())
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(self.store.pending()[0]["incident_id"], "INC-temp-only")


if __name__ == "__main__": unittest.main()
