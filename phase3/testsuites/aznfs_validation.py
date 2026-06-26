# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
"""
AzNFS Package Validation Test Suite (Phase 3)
=============================================

Validates the AzNFS package on a freshly provisioned distro VM, following the
5-tier plan agreed with the team:

  Tier 1 - Artifact integrity   : signature / metadata / dependencies of the
                                  downloaded package (static, pre-install).
  Tier 2 - Install lifecycle    : clean install, reinstall (idempotency),
                                  clean uninstall.
  Tier 3 - Post-install footprint: package registered at the right version,
                                  files intact, ``mount.aznfs`` AVAILABLE
                                  (it is a mount helper, NOT a daemon), and the
                                  ``aznfswatchdog`` daemon RUNNING.
  Tier 4 - Functional           : mount an Azure Files NFS share via AzNFS and
                                  do a simple create/write/read (with and
                                  without EIT). Deliberately minimal - no heavy
                                  / stress I/O (per Shyam's guidance).
  Tier 5 - Resilience (basic)    : after a successful mount, restart/kill the
                                  ``aznfswatchdog`` service and verify the mount
                                  still works and the service recovers.

These tiers are grouped into three test cases (one VM each) for cost:
  * verify_aznfs_install_lifecycle  -> Tiers 1-3 (no share needed)
  * verify_aznfs_nfs_functional     -> Tier 4   (needs an NFS share)
  * verify_aznfs_resilience         -> Tier 5   (needs an NFS share)

ASSUMPTIONS TO CONFIRM WITH THE TEAM (kept as runbook variables so they can be
changed without touching code):
  * Package name in the OS package DB is ``aznfs``.
  * The watchdog systemd service is named ``aznfswatchdog``.
  * The mount helper binary is ``mount.aznfs``.
  * AzNFS mounts Azure Files NFS via ``mount -t aznfs`` with NFSv4.1 options.
  * The exact EIT (encryption-in-transit) mount option is supplied via the
    ``aznfs_eit_mount_opts`` runbook variable (empty => EIT variant skipped).
"""
import time
from typing import Any, Optional, Tuple, cast

from assertpy import assert_that

from lisa import (
    Logger,
    RemoteNode,
    SkippedException,
    TestCaseMetadata,
    TestSuite,
    TestSuiteMetadata,
    UnsupportedDistroException,
    simple_requirement,
)
from lisa.environment import Environment
from lisa.operating_system import BSD, CBLMariner, Debian, Redhat, Suse, Windows
from lisa.sut_orchestrator import AZURE
from lisa.sut_orchestrator.azure.features import Nfs
from lisa.sut_orchestrator.azure.platform_ import AzurePlatform
from lisa.testsuite import TestResult
from lisa.tools import Service, Wget
from lisa.util import constants

# =============================================================================
# Module-level configuration (AzNFS-specific names; confirm with team)
# =============================================================================
AZNFS_PACKAGE_NAME = "aznfs"
AZNFS_WATCHDOG_SERVICE = "aznfswatchdog"
AZNFS_MOUNT_HELPER = "mount.aznfs"

# Local mount point on the test VM.
_MOUNT_POINT = "/mnt/aznfs"

# Defaults overridable via runbook variables (see before_case).
_DEFAULT_MOUNT_TYPE = "aznfs"
_DEFAULT_MOUNT_OPTS = "vers=4,minorversion=1,sec=sys"

# A small, fixed payload used to prove the share is readable/writable.
_PROBE_FILE = f"{_MOUNT_POINT}/aznfs_probe.txt"
_PROBE_CONTENT = "aznfs-validation-probe"

_TIME_OUT = 3600

# A freshly created Azure Files NFS share can briefly reject mounts with
# "mount.nfs: access denied by server" until its export is ready (observed right
# after create_share()). The mount itself is correct - just retry with a delay.
_MOUNT_RETRIES = 8
_MOUNT_RETRY_DELAY = 30

# A freshly booted Debian/Ubuntu VM may still be running cloud-init's first apt
# refresh (holding the apt lists lock), so retry apt-get update to ride it out.
_APT_UPDATE_RETRIES = 5
_APT_RETRY_DELAY = 20

# Package install can be slow on a freshly booted VM: it may wait for the
# distro's boot-time package-manager lock to release, then refresh repo
# metadata and download dependencies (stunnel, nfs-utils, ...). Give it room.
_INSTALL_TIMEOUT = 1800


@TestSuiteMetadata(
    area="storage",
    category="functional",
    description="""
    Validates the AzNFS package on a new Linux distro: artifact integrity,
    install lifecycle, post-install footprint, a simple NFS mount + I/O check,
    and basic watchdog resilience. This is package validation - not a heavy
    functional/stress workload.
    """,
)
class AzNfsValidation(TestSuite):
    # Runbook-provided configuration (defaults here so the type checker is happy
    # and the suite still runs if a variable is omitted). Populated in
    # before_case().
    _package_url: str = ""
    _pmc_repo: str = ""
    _expected_version: str = ""
    _mount_type: str = _DEFAULT_MOUNT_TYPE
    _mount_opts: str = _DEFAULT_MOUNT_OPTS
    _eit_mount_opts: str = ""

    def before_case(self, log: Logger, **kwargs: Any) -> None:
        variables: dict = kwargs["variables"]
        # Phase 2 hands off a PMC prod download URL for the exact package; that
        # is the primary install source. If empty, fall back to a PMC repo.
        self._package_url = variables.get("aznfs_package_url", "")
        self._pmc_repo = variables.get("aznfs_pmc_repo", "")
        self._expected_version = variables.get("aznfs_expected_version", "")
        self._mount_type = variables.get("aznfs_mount_type", _DEFAULT_MOUNT_TYPE)
        self._mount_opts = variables.get("aznfs_mount_opts", _DEFAULT_MOUNT_OPTS)
        # Empty by default: the EIT mount variant is only exercised when the
        # team provides the exact EIT mount option string.
        self._eit_mount_opts = variables.get("aznfs_eit_mount_opts", "")

    # -------------------------------------------------------------------------
    # Test cases
    # -------------------------------------------------------------------------
    @TestCaseMetadata(
        description="""
        Tiers 1-3: artifact integrity, install lifecycle (install, reinstall,
        uninstall) and post-install footprint. No Azure file share is needed.
        """,
        requirement=simple_requirement(
            min_core_count=2,
            supported_platform_type=[AZURE],
            unsupported_os=[BSD, Windows],
        ),
        timeout=_TIME_OUT,
        use_new_environment=True,
        priority=2,
    )
    def verify_aznfs_install_lifecycle(
        self, log: Logger, result: TestResult
    ) -> None:
        _, node = self._get_node(result)
        self._check_supported_distro(node)

        # --- Tier 1: artifact integrity (only possible when we have the file) ---
        if self._package_url:
            package_path = self._download_package(node)
            self._verify_artifact(node, package_path, log)
            self._install_package_file(node, package_path, log)
        else:
            log.info("No aznfs_package_url provided; installing from PMC repo.")
            self._install_from_pmc(node)

        # --- Tier 3: footprint (verify the install landed correctly) ---
        self._verify_footprint(node, log)

        # --- Tier 2: reinstall / idempotency ---
        log.info("Re-installing aznfs to verify idempotency.")
        if self._package_url:
            self._install_package_file(node, self._download_package(node), log)
        else:
            self._install_from_pmc(node)
        self._verify_footprint(node, log)

        # --- Tier 2: clean uninstall ---
        log.info("Uninstalling aznfs.")
        node.os.uninstall_packages(AZNFS_PACKAGE_NAME)
        assert_that(node.os.package_exists(AZNFS_PACKAGE_NAME)).described_as(
            "[Tier 2: install] aznfs should not be registered after uninstall"
        ).is_false()

        # NOTE (team follow-up): upgrade-from-previous and the AzNFS auto-update
        # feature still need a known prior version + reachable update channel;
        # left out of v1 pending confirmation.

    @TestCaseMetadata(
        description="""
        Tier 4: install AzNFS, mount an Azure Files NFS share through it, and do
        a simple create/write/read/delete to confirm the share is accessible.
        Runs once without EIT and once with EIT (if an EIT mount option string
        is provided). Minimal by design - no heavy/stress I/O.
        """,
        requirement=simple_requirement(
            min_core_count=2,
            supported_platform_type=[AZURE],
            unsupported_os=[BSD, Windows],
        ),
        timeout=_TIME_OUT,
        use_new_environment=True,
        priority=2,
    )
    def verify_aznfs_nfs_functional(self, log: Logger, result: TestResult) -> None:
        environment, node = self._get_node(result)
        assert isinstance(environment.platform, AzurePlatform)
        self._check_supported_distro(node)
        self._install_aznfs(node, log)
        self._verify_footprint(node, log)

        # One mount variant without EIT, plus an EIT variant if configured.
        variants = [("without EIT", self._mount_opts)]
        if self._eit_mount_opts:
            variants.append(("with EIT", self._eit_mount_opts))

        nfs: Optional[Nfs] = None
        test_failed = False
        try:
            for label, opts in variants:
                log.info(f"Functional mount {label}: options='{opts}'")
                nfs = self._provision_and_mount(node, environment, opts, log)
                self._simple_io(node, log)
                self._unmount(node)
                self._cleanup_share(nfs, environment, test_failed=False, log=log)
                nfs = None
        except Exception:
            test_failed = True
            raise
        finally:
            self._teardown(node, nfs, environment, test_failed, log)

    @TestCaseMetadata(
        description="""
        Tier 5 (basic resilience): after a successful mount, restart/kill the
        aznfswatchdog service and verify the mount still serves I/O and the
        service comes back to active. Single account / single share.
        """,
        requirement=simple_requirement(
            min_core_count=2,
            supported_platform_type=[AZURE],
            unsupported_os=[BSD, Windows],
        ),
        timeout=_TIME_OUT,
        use_new_environment=True,
        priority=3,
    )
    def verify_aznfs_resilience(self, log: Logger, result: TestResult) -> None:
        environment, node = self._get_node(result)
        assert isinstance(environment.platform, AzurePlatform)
        self._check_supported_distro(node)
        self._install_aznfs(node, log)
        self._verify_footprint(node, log)

        service = node.tools[Service]
        nfs: Optional[Nfs] = None
        test_failed = False
        try:
            # Baseline: mount works and I/O succeeds.
            nfs = self._provision_and_mount(node, environment, self._mount_opts, log)
            self._simple_io(node, log)

            # Disrupt: restart the watchdog (simplest sufficient disruption).
            log.info(f"Restarting {AZNFS_WATCHDOG_SERVICE} to test resilience.")
            service.restart_service(AZNFS_WATCHDOG_SERVICE)

            # The existing mount must still serve I/O after the restart.
            self._simple_io(node, log)

            # The watchdog must recover to active.
            assert_that(
                service.is_service_running(AZNFS_WATCHDOG_SERVICE)
            ).described_as(
                f"[Tier 5: watchdog] {AZNFS_WATCHDOG_SERVICE} should be active after restart"
            ).is_true()

            self._unmount(node)
        except Exception:
            test_failed = True
            raise
        finally:
            self._teardown(node, nfs, environment, test_failed, log)

    # -------------------------------------------------------------------------
    # Helpers - environment / distro
    # -------------------------------------------------------------------------
    def _get_node(self, result: TestResult) -> Tuple[Environment, RemoteNode]:
        environment = result.environment
        assert environment, "fail to get environment from test result"
        node = cast(RemoteNode, environment.nodes[0])
        return environment, node

    def _check_supported_distro(self, node: RemoteNode) -> None:
        # AzNFS ships .rpm (Redhat/Oracle, SLES, Azure Linux) and .deb
        # (Debian/Ubuntu). Anything else is skipped, not failed.
        if not isinstance(node.os, (Redhat, Suse, CBLMariner, Debian)):
            raise SkippedException(
                UnsupportedDistroException(
                    node.os, "AzNFS supports RPM/DEB-based distros only"
                )
            )

    # -------------------------------------------------------------------------
    # Helpers - install
    # -------------------------------------------------------------------------
    def _download_package(self, node: RemoteNode) -> str:
        # Wget caches by URL, so repeated calls return the same local path.
        return node.tools[Wget].get(url=self._package_url, sudo=True)

    def _install_aznfs(self, node: RemoteNode, log: Logger) -> None:
        """Install AzNFS (used by the functional/resilience cases)."""
        if self._package_url:
            self._install_package_file(node, self._download_package(node), log)
        else:
            self._install_from_pmc(node)

    def _install_package_file(
        self, node: RemoteNode, package_path: str, log: Logger
    ) -> None:
        """Install a downloaded .rpm/.deb file, resolving dependencies.

        signed=False skips GPG checks for the local file; the artifact
        signature was already verified in Tier 1.
        """
        self._prepare_package_manager(node)
        self._install_target(node, package_path, signed=False)
        log.info(f"Installed aznfs from {package_path}")

    def _install_from_pmc(self, node: RemoteNode) -> None:
        """Fallback path: add the PMC repo and install aznfs by name."""
        self._prepare_package_manager(node)
        if self._pmc_repo:
            # add_repository's exact signature varies by distro family; the
            # repo string itself differs per distro (ubuntu/rhel/sles paths).
            node.os.add_repository(repo=self._pmc_repo)
        self._install_target(node, AZNFS_PACKAGE_NAME, signed=True)

    def _install_target(
        self, node: RemoteNode, target: str, signed: bool
    ) -> None:
        """Install an aznfs package (local file path or repo package name).

        Why this does not use ``node.os.install_packages``: the aznfs
        post-install scriptlet shows an INTERACTIVE whiptail dialog ("Enable
        auto update for AZNFS mount helper") written to ``/dev/tty``. LISA
        allocates a pseudo-terminal, so the dialog renders and blocks forever
        waiting for keyboard input - observed as the install hanging for the
        whole timeout on ``Running scriptlet: aznfs``. The package skips that
        prompt when ``AZNFS_NONINTERACTIVE_INSTALL=1`` is set, but
        ``install_packages`` cannot inject environment variables. So we invoke
        the package manager directly with ``update_envs`` - LISA exports them
        inside the elevated shell (``sudo sh -c 'export VAR=...; <cmd>'``), so
        the rpm/deb scriptlet inherits them. ``DEBIAN_FRONTEND`` keeps
        apt/debconf non-interactive on Debian as well.

        RHUI prep (``_prepare_package_manager``) is run by the callers, so the
        RHEL repos are healthy before we get here.
        """
        envs = {
            "AZNFS_NONINTERACTIVE_INSTALL": "1",
            "DEBIAN_FRONTEND": "noninteractive",
        }
        if isinstance(node.os, Debian):
            command = f"apt-get install -y {target}"
        elif isinstance(node.os, Suse):
            unsigned = "" if signed else "--allow-unsigned-rpm "
            command = f"zypper --non-interactive install {unsigned}{target}"
        else:  # Redhat / CBLMariner (dnf/yum family)
            unsigned = "" if signed else " --nogpgcheck"
            command = f"yum install -y {target}{unsigned}"
        result = node.execute(
            command,
            sudo=True,
            shell=True,
            update_envs=envs,
            timeout=_INSTALL_TIMEOUT,
        )
        result.assert_exit_code(
            0, f"[Tier 2: install] aznfs install command failed:\n{result.stdout}"
        )

    def _prepare_package_manager(self, node: RemoteNode) -> None:
        """Refresh package metadata before installing aznfs.

        RHEL: some marketplace images ship an out-of-date ``rhui-azure-*``
        client whose Azure RHUI repos reject metadata with HTTP 400, breaking
        dependency resolution. ``handle_rhui_issue()`` updates the client and
        ``yum clean all`` drops stale cached metadata.

        Debian/Ubuntu: the aznfs ``.deb`` declares ``Depends: stunnel4``, which
        apt can only resolve once the package lists are populated. A freshly
        booted Azure VM's lists are stale/empty (cloud-init's first refresh may
        not have finished), so ``apt-get install`` of the local file fails with
        "stunnel4 ... not installable". Refresh the lists first, retrying to
        ride out the boot-time apt-lists lock held by cloud-init.

        No-op on other distros.
        """
        if isinstance(node.os, Redhat):
            node.os.handle_rhui_issue()
            node.execute("yum clean all", sudo=True, shell=True, timeout=120)
        elif isinstance(node.os, Debian):
            envs = {"DEBIAN_FRONTEND": "noninteractive"}
            for attempt in range(1, _APT_UPDATE_RETRIES + 1):
                result = node.execute(
                    "apt-get update -y",
                    sudo=True,
                    shell=True,
                    update_envs=envs,
                    timeout=600,
                )
                if result.exit_code == 0:
                    break
                if attempt < _APT_UPDATE_RETRIES:
                    time.sleep(_APT_RETRY_DELAY)

    # -------------------------------------------------------------------------
    # Helpers - Tier 1 (artifact) and Tier 3 (footprint)
    # -------------------------------------------------------------------------
    def _verify_artifact(
        self, node: RemoteNode, package_path: str, log: Logger
    ) -> None:
        if isinstance(node.os, Debian):
            # Metadata + declared dependencies for a .deb.
            info = node.execute(f"dpkg-deb -I {package_path}", sudo=True, shell=True)
            info.assert_exit_code(0, "[Tier 1: artifact] failed to read .deb metadata")
            assert_that(info.stdout.lower()).described_as(
                "[Tier 1: artifact] package metadata should mention aznfs"
            ).contains(AZNFS_PACKAGE_NAME)
        else:
            # Signature/digest. NOTE: `rpm -K` returns 0 when digests are OK
            # even if the GPG key isn't imported; the team can tighten this to
            # require "signatures OK" once the Microsoft key is trusted.
            sig = node.execute(f"rpm -K {package_path}", sudo=True, shell=True)
            log.info(f"rpm -K output: {sig.stdout.strip()}")
            sig.assert_exit_code(0, "[Tier 1: artifact] rpm signature/digest check failed")
            # Metadata.
            info = node.execute(f"rpm -qpi {package_path}", sudo=True, shell=True)
            assert_that(info.stdout.lower()).described_as(
                "[Tier 1: artifact] package metadata should mention aznfs"
            ).contains(AZNFS_PACKAGE_NAME)
            # Declared dependencies (logged for visibility).
            deps = node.execute(f"rpm -qpR {package_path}", sudo=True, shell=True)
            log.info(f"aznfs declared dependencies:\n{deps.stdout}")

        if self._expected_version:
            assert_that(package_path).described_as(
                "[Tier 1: artifact] downloaded package filename should contain the expected version"
            ).contains(self._expected_version)

    def _verify_footprint(self, node: RemoteNode, log: Logger) -> None:
        # Registered in the package DB.
        assert_that(node.os.package_exists(AZNFS_PACKAGE_NAME)).described_as(
            "[Tier 3: footprint] aznfs should be registered after install"
        ).is_true()

        # Correct/expected version.
        if self._expected_version:
            if isinstance(node.os, Debian):
                query = (
                    f"dpkg-query -W -f='${{Version}}' {AZNFS_PACKAGE_NAME}"
                )
            else:
                query = (
                    "rpm -q --queryformat '%{VERSION}-%{RELEASE}' "
                    f"{AZNFS_PACKAGE_NAME}"
                )
            installed = node.execute(query, sudo=True, shell=True).stdout
            assert_that(installed).described_as(
                "[Tier 3: footprint] installed aznfs version should match expected"
            ).contains(self._expected_version)

        # Files intact (rpm family only). rpm -V reports config-file changes as
        # well, which are normal, so we log rather than hard-fail.
        if not isinstance(node.os, Debian):
            verify = node.execute(
                f"rpm -V {AZNFS_PACKAGE_NAME}", sudo=True, shell=True
            )
            log.info(f"rpm -V output (empty == all files intact): '{verify.stdout}'")

        # mount.aznfs is a mount HELPER, not a daemon: assert it is AVAILABLE.
        which = node.execute(f"which {AZNFS_MOUNT_HELPER}", sudo=True, shell=True)
        which.assert_exit_code(
            0, f"[Tier 3: footprint] {AZNFS_MOUNT_HELPER} should be available on PATH"
        )

        # aznfswatchdog IS a daemon: assert it is RUNNING.
        assert_that(
            node.tools[Service].is_service_running(AZNFS_WATCHDOG_SERVICE)
        ).described_as(
            f"[Tier 3: footprint] {AZNFS_WATCHDOG_SERVICE} daemon should be running"
        ).is_true()

    # -------------------------------------------------------------------------
    # Helpers - Tier 4 (mount + I/O)
    # -------------------------------------------------------------------------
    def _provision_and_mount(
        self,
        node: RemoteNode,
        environment: Environment,
        mount_opts: str,
        log: Logger,
    ) -> Nfs:
        """Create an Azure Files NFS share and mount it via AzNFS."""
        nfs = node.features[Nfs]
        nfs.create_share()

        # Azure Files NFS export: <account>.file.core.windows.net:/<account>/<share>
        # The private endpoint + private DNS created by create_share() make this
        # hostname resolve to the private IP from inside the VM.
        source = (
            f"{nfs.storage_account_name}.file.core.windows.net:"
            f"/{nfs.storage_account_name}/{nfs.file_share_name}"
        )
        node.execute(f"mkdir -p {_MOUNT_POINT}", sudo=True, shell=True)
        mount_cmd = (
            f"mount -t {self._mount_type} {source} {_MOUNT_POINT} -o {mount_opts}"
        )
        # A newly created Azure Files NFS share can return "access denied by
        # server" for a short window after creation, until the export is ready.
        # Retry with a delay before failing (the mount command itself is fine).
        mount_result = node.execute(mount_cmd, sudo=True, shell=True)
        for attempt in range(1, _MOUNT_RETRIES + 1):
            if mount_result.exit_code == 0:
                break
            log.info(
                f"mount attempt {attempt}/{_MOUNT_RETRIES} failed "
                f"(exit {mount_result.exit_code}): {mount_result.stdout.strip()}; "
                f"retrying in {_MOUNT_RETRY_DELAY}s (share may be warming up)"
            )
            time.sleep(_MOUNT_RETRY_DELAY)
            mount_result = node.execute(mount_cmd, sudo=True, shell=True)
        mount_result.assert_exit_code(
            0, f"[Tier 4: mount] failed to mount {source} via {self._mount_type}"
        )
        log.info(f"Mounted {source} at {_MOUNT_POINT}")
        return nfs

    def _simple_io(self, node: RemoteNode, log: Logger) -> None:
        """Minimal accessibility check: write a file, read it back, delete it."""
        node.execute(
            f"echo '{_PROBE_CONTENT}' | tee {_PROBE_FILE}",
            sudo=True,
            shell=True,
            expected_exit_code=0,
            expected_exit_code_failure_message="[Tier 4: io] failed to write probe file",
        )
        read_back = node.execute(f"cat {_PROBE_FILE}", sudo=True, shell=True)
        assert_that(read_back.stdout).described_as(
            "[Tier 4: io] probe file content read back from the share"
        ).contains(_PROBE_CONTENT)
        node.execute(f"rm -f {_PROBE_FILE}", sudo=True, shell=True)
        log.info("Simple create/write/read/delete succeeded on the share.")

    def _unmount(self, node: RemoteNode) -> None:
        node.execute(f"umount {_MOUNT_POINT}", sudo=True, shell=True)

    # -------------------------------------------------------------------------
    # Helpers - cleanup
    # -------------------------------------------------------------------------
    def _teardown(
        self,
        node: RemoteNode,
        nfs: Optional[Nfs],
        environment: Environment,
        test_failed: bool,
        log: Logger,
    ) -> None:
        if nfs is None:
            return
        try:
            self._unmount(node)
        except Exception:
            pass  # best-effort: may already be unmounted
        self._cleanup_share(nfs, environment, test_failed, log)

    def _cleanup_share(
        self,
        nfs: Nfs,
        environment: Environment,
        test_failed: bool,
        log: Logger,
    ) -> None:
        # Respect keep_environment so a failed run can be inspected.
        should_cleanup = True
        if environment.platform:
            keep = environment.platform.runbook.keep_environment
            if keep == constants.ENVIRONMENT_KEEP_ALWAYS:
                should_cleanup = False
            elif keep == constants.ENVIRONMENT_KEEP_FAILED and test_failed:
                should_cleanup = False
        if not should_cleanup:
            log.info("Skipping NFS share cleanup due to keep_environment setting.")
            return
        try:
            nfs.delete_share()
        except Exception as e:
            log.error(f"failed to delete NFS share: {e}")
