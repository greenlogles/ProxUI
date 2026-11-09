#!/usr/bin/env python3
"""
Tests for BIOS/firmware selection (SeaBIOS vs OVMF) on VM and cloud template creation.
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from app import app as flask_app

HYBRID_IMAGE = "debian-13"
BIOS_ONLY_IMAGE = "openbsd-7.8-hcartiaux-mirror"


class TestFirmwareHelpers(unittest.TestCase):
    def test_efidisk_spec_without_secure_boot(self):
        self.assertEqual(
            app.efidisk_spec("local-lvm", False),
            "local-lvm:1,efitype=4m,pre-enrolled-keys=0",
        )

    def test_efidisk_spec_with_secure_boot(self):
        self.assertEqual(
            app.efidisk_spec("local-lvm", True),
            "local-lvm:1,efitype=4m,pre-enrolled-keys=1",
        )

    def test_catalog_image_without_key_is_hybrid(self):
        self.assertEqual(
            app.cloud_image_firmware(app.CLOUD_IMAGES[HYBRID_IMAGE]),
            ["seabios", "ovmf"],
        )

    def test_catalog_bios_only_image(self):
        self.assertEqual(
            app.cloud_image_firmware(app.CLOUD_IMAGES[BIOS_ONLY_IMAGE]), ["seabios"]
        )


class TestCreateVmFirmware(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        self.proxmox = Mock()
        self.patchers = [
            patch("app.get_proxmox_connection", return_value=self.proxmox),
            patch("app.config_file_exists", True),
            patch.dict("app.all_clusters", {"c1": {"id": "c1"}}),
        ]
        for p in self.patchers:
            p.start()
        self.form = {
            "node": "pve1",
            "type": "qemu",
            "creation_method": "blank",
            "name": "vm1",
            "vmid": "150",
            "storage": "local-lvm",
            "disk_size": "20",
            "bridge": "vmbr0",
        }

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def _create_kwargs(self):
        create = self.proxmox.nodes.return_value.qemu.create
        create.assert_called_once()
        return create.call_args.kwargs

    def test_default_is_seabios_without_efidisk(self):
        self.client.post("/create", data=self.form)
        kwargs = self._create_kwargs()
        self.assertNotIn("bios", kwargs)
        self.assertNotIn("efidisk0", kwargs)
        self.assertNotIn("machine", kwargs)

    def test_ovmf_adds_efidisk_on_vm_storage(self):
        self.client.post("/create", data={**self.form, "bios": "ovmf"})
        kwargs = self._create_kwargs()
        self.assertEqual(kwargs["bios"], "ovmf")
        self.assertEqual(
            kwargs["efidisk0"], "local-lvm:1,efitype=4m,pre-enrolled-keys=0"
        )
        self.assertEqual(kwargs["machine"], "q35")

    def test_ovmf_secure_boot(self):
        self.client.post(
            "/create", data={**self.form, "bios": "ovmf", "secure_boot": "1"}
        )
        self.assertTrue(self._create_kwargs()["efidisk0"].endswith("keys=1"))

    def test_invalid_bios_rejected(self):
        self.client.post("/create", data={**self.form, "bios": "coreboot"})
        self.proxmox.nodes.return_value.qemu.create.assert_not_called()


class TestCloudTemplateFirmware(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        self.proxmox = Mock()
        self.patchers = [
            patch("app.get_proxmox_connection", return_value=self.proxmox),
            patch("app.config_file_exists", True),
            patch.dict("app.all_clusters", {"c1": {"id": "c1"}}),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def _post(self, **extra):
        body = {"image_id": HYBRID_IMAGE, "storage": "local-lvm", "vmid": 9000}
        body.update(extra)
        with patch("app.threading.Thread") as thread:
            resp = self.client.post("/api/node/pve1/create-cloud-template", json=body)
        return resp, thread

    def test_ovmf_passed_to_job(self):
        resp, thread = self._post(bios="ovmf", secure_boot=True)
        self.assertEqual(resp.status_code, 200)
        params = thread.call_args.kwargs["args"][2]
        self.assertEqual(params["bios"], "ovmf")
        self.assertTrue(params["secure_boot"])

    def test_default_bios_is_seabios(self):
        resp, thread = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(thread.call_args.kwargs["args"][2]["bios"], "seabios")

    def test_ovmf_rejected_for_bios_only_image(self):
        resp, thread = self._post(image_id=BIOS_ONLY_IMAGE, bios="ovmf")
        self.assertEqual(resp.status_code, 400)
        thread.assert_not_called()

    def test_invalid_bios_rejected(self):
        resp, thread = self._post(bios="coreboot")
        self.assertEqual(resp.status_code, 400)
        thread.assert_not_called()

    def test_api_cloud_images_exposes_firmware(self):
        images = {i["id"]: i for i in self.client.get("/api/cloud-images").get_json()}
        self.assertEqual(images[BIOS_ONLY_IMAGE]["firmware"], ["seabios"])
        self.assertEqual(images[HYBRID_IMAGE]["firmware"], ["seabios", "ovmf"])


class TestCloudTemplateJobFirmware(unittest.TestCase):
    """The job must create the VM with bios/efidisk0 in the initial create call."""

    def _run_job(self, bios, secure_boot=False):
        proxmox = Mock()
        node_api = proxmox.nodes.return_value
        node_api.storage.get.return_value = [
            {"storage": "local", "active": 1, "content": "iso,import"}
        ]
        filename = app.filename_from_url(app.CLOUD_IMAGES[HYBRID_IMAGE]["url"])
        node_api.storage.return_value.content.get.return_value = [
            {"volid": f"local:import/{filename}"}
        ]
        params = {
            "image_id": HYBRID_IMAGE,
            "storage": "local-lvm",
            "vmid": 9000,
            "name": "t",
            "cores": 2,
            "memory": 2048,
            "disk_size": "10G",
            "bridge": "vmbr0",
            "bios": bios,
            "secure_boot": secure_boot,
        }
        job_id = app.job_queue.create_job(
            job_type="create_cloud_template", description="t", params=params
        )
        # Stop the job right after the create call.
        with patch("app.get_proxmox_connection", return_value=proxmox), patch(
            "app.CLOUD_IMAGE_CACHE_MODE", "REUSE"
        ), patch("app.wait_for_task", return_value={"success": False, "error": "stop"}):
            app.run_cloud_template_job(job_id, "pve1", params)
        node_api.qemu.create.assert_called_once()
        return node_api.qemu.create.call_args.kwargs

    def test_ovmf_job_creates_efidisk(self):
        kwargs = self._run_job("ovmf", secure_boot=True)
        self.assertEqual(kwargs["bios"], "ovmf")
        self.assertEqual(
            kwargs["efidisk0"], "local-lvm:1,efitype=4m,pre-enrolled-keys=1"
        )
        self.assertEqual(kwargs["machine"], "q35")

    def test_seabios_job_has_no_efidisk(self):
        kwargs = self._run_job("seabios")
        self.assertNotIn("bios", kwargs)
        self.assertNotIn("efidisk0", kwargs)
        self.assertNotIn("machine", kwargs)


class TestVmBiosEdit(unittest.TestCase):
    EFIDISK = "local-lvm:vm-150-disk-1,efitype=4m,pre-enrolled-keys=0,size=4M"

    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        self.proxmox = Mock()
        self.vm = self.proxmox.nodes.return_value.qemu.return_value
        self.vm.status.current.get.return_value = {"status": "stopped"}
        self.patchers = [
            patch("app.get_proxmox_connection", return_value=self.proxmox),
            patch("app.config_file_exists", True),
            patch.dict("app.all_clusters", {"c1": {"id": "c1"}}),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def _put(self, **body):
        return self.client.put("/api/vm/pve1/150/bios", json=body)

    def _put_calls(self):
        return [c.kwargs for c in self.vm.config.put.call_args_list]

    def test_to_ovmf_creates_efidisk(self):
        self.vm.config.get.return_value = {"scsi0": "local-lvm:vm-150-disk-0"}
        resp = self._put(bios="ovmf", storage="local-lvm", secure_boot=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self._put_calls(),
            [
                {
                    "bios": "ovmf",
                    "efidisk0": "local-lvm:1,efitype=4m,pre-enrolled-keys=1",
                }
            ],
        )

    def test_to_ovmf_reuses_existing_efidisk(self):
        self.vm.config.get.return_value = {"efidisk0": self.EFIDISK}
        resp = self._put(bios="ovmf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._put_calls(), [{"bios": "ovmf"}])

    def test_to_ovmf_without_storage_rejected(self):
        self.vm.config.get.return_value = {}
        self.assertEqual(self._put(bios="ovmf").status_code, 400)
        self.vm.config.put.assert_not_called()

    def test_to_seabios_keeps_efidisk_by_default(self):
        self.vm.config.get.return_value = {"bios": "ovmf", "efidisk0": self.EFIDISK}
        resp = self._put(bios="seabios")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._put_calls(), [{"delete": "bios"}])

    def test_to_seabios_deletes_efidisk_volume(self):
        self.vm.config.get.side_effect = [
            {"bios": "ovmf", "efidisk0": self.EFIDISK},
            {
                "unused0": "local-lvm:vm-150-disk-9",
                "unused1": "local-lvm:vm-150-disk-1",
            },
        ]
        resp = self._put(bios="seabios", remove_efidisk=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self._put_calls(), [{"delete": "bios,efidisk0"}, {"delete": "unused1"}]
        )
        self.assertIn("deleted", resp.get_json()["message"])

    def test_to_seabios_volume_still_referenced(self):
        self.vm.config.get.side_effect = [
            {"bios": "ovmf", "efidisk0": self.EFIDISK},
            {},
        ]
        resp = self._put(bios="seabios", remove_efidisk=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._put_calls(), [{"delete": "bios,efidisk0"}])
        self.assertIn("kept", resp.get_json()["message"])

    def test_volume_delete_failure_reported(self):
        self.vm.config.get.side_effect = [
            {"bios": "ovmf", "efidisk0": self.EFIDISK},
            {"unused0": "local-lvm:vm-150-disk-1"},
        ]
        self.vm.config.put.side_effect = [None, Exception("used by linked clone")]
        resp = self._put(bios="seabios", remove_efidisk=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("kept as unused0", resp.get_json()["message"])

    def test_running_vm_rejected(self):
        self.vm.status.current.get.return_value = {"status": "running"}
        self.assertEqual(self._put(bios="ovmf", storage="x").status_code, 409)
        self.vm.config.put.assert_not_called()

    def test_invalid_bios_rejected(self):
        self.assertEqual(self._put(bios="coreboot").status_code, 400)

    def test_edit_page_renders_bios_card(self):
        self.vm.status.current.get.return_value = {"status": "stopped", "vmid": 150}
        self.vm.config.get.return_value = {
            "bios": "ovmf",
            "efidisk0": self.EFIDISK,
            "ide2": "local:iso/x.iso,media=cdrom",
            "scsi0": "fast:vm-150-disk-0,size=10G",
        }
        html = self.client.get("/vm/pve1/150/edit").get_data(as_text=True)
        self.assertIn("/api/vm/pve1/150/bios", html)
        self.assertIn("storage: 'fast'", html)
        self.assertIn("efidisk: 'local-lvm:vm-150-disk-1'", html)
        self.assertIn("/api/vm/pve1/150/machine", html)
        self.assertIn("currentMachine: 'i440fx'", html)


class TestMachineHelpers(unittest.TestCase):
    def test_family(self):
        cases = {
            None: "i440fx",
            "": "i440fx",
            "pc": "i440fx",
            "pc-i440fx-8.1+pve0": "i440fx",
            "q35": "q35",
            "pc-q35-9.2+pve1,viommu=intel": "q35",
            "type=q35,enable-s3=1": "q35",
            "viommu=virtio": "i440fx",
        }
        for value, family in cases.items():
            self.assertEqual(app.machine_family(value), family, value)

    def test_split_keeps_options(self):
        self.assertEqual(
            app.split_machine("type=pc-q35-9.2,viommu=intel,enable-s3=0"),
            ("pc-q35-9.2", ["viommu=intel", "enable-s3=0"]),
        )


class TestVmMachineEdit(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        self.proxmox = Mock()
        self.vm = self.proxmox.nodes.return_value.qemu.return_value
        self.vm.status.current.get.return_value = {"status": "stopped"}
        self.patchers = [
            patch("app.get_proxmox_connection", return_value=self.proxmox),
            patch("app.config_file_exists", True),
            patch.dict("app.all_clusters", {"c1": {"id": "c1"}}),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def _put(self, current, machine):
        self.vm.config.get.return_value = (
            {} if current is None else {"machine": current}
        )
        return self.client.put("/api/vm/pve1/150/machine", json={"machine": machine})

    def _put_calls(self):
        return [c.kwargs for c in self.vm.config.put.call_args_list]

    def test_default_to_q35(self):
        self.assertEqual(self._put(None, "q35").status_code, 200)
        self.assertEqual(self._put_calls(), [{"machine": "q35"}])

    def test_q35_to_i440fx_deletes_machine(self):
        self.assertEqual(self._put("pc-q35-9.2+pve0", "i440fx").status_code, 200)
        self.assertEqual(self._put_calls(), [{"delete": "machine"}])

    def test_options_are_kept(self):
        self._put("pc-i440fx-8.1,viommu=virtio", "q35")
        self._put("q35,viommu=virtio", "i440fx")
        self.assertEqual(
            self._put_calls(),
            [{"machine": "q35,viommu=virtio"}, {"machine": "pc,viommu=virtio"}],
        )

    def test_same_family_is_noop(self):
        resp = self._put("pc-q35-9.2+pve0", "q35")
        self.assertEqual(resp.status_code, 200)
        self.vm.config.put.assert_not_called()

    def test_running_vm_rejected(self):
        self.vm.status.current.get.return_value = {"status": "running"}
        self.assertEqual(self._put(None, "q35").status_code, 409)
        self.vm.config.put.assert_not_called()

    def test_invalid_machine_rejected(self):
        self.assertEqual(self._put(None, "virt").status_code, 400)
        self.vm.config.put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
