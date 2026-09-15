"""Fail-closed compatibility profiles for native FIFA 19 builds.

The FUT service core is intentionally build-independent.  This module is the
only place that identifies installed game binaries and maps them to a native
adapter.  A process must match every declared binary before Frida is allowed
to attach; filename or file-version matches alone are never sufficient.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class BinarySpec:
    relative_path: str
    size: int
    sha256: str
    file_version: str
    product_version: str


@dataclass(frozen=True)
class BuildProfile:
    profile_id: str
    display_name: str
    native_adapter_id: str
    route_adapter_id: str
    dto_adapter_id: str
    feature_flags: frozenset[str]
    executable: BinarySpec
    modules: tuple[BinarySpec, ...]
    certificate_handler_rva: int
    certificate_success_value: int


@dataclass(frozen=True)
class KnownBuildProfile:
    """Exact build identity plus its user-facing launch maturity.

    Known does not mean supported.  Observed profiles are deliberately kept
    outside ``SUPPORTED_BUILD_PROFILES`` and cannot select a native adapter.
    """

    profile_id: str
    display_name: str
    support_status: str
    launch_strategy: str
    selection_priority: int
    feature_flags: frozenset[str]
    executable: BinarySpec
    modules: tuple[BinarySpec, ...]


@dataclass(frozen=True)
class RuntimeAdapterProfile:
    """Explicit runtime layers selected after exact build detection."""

    profile_id: str
    native_adapter_id: str
    route_adapter_id: str
    dto_adapter_id: str
    data_namespace: str
    server_mode: str


@dataclass(frozen=True)
class BinaryFingerprint:
    relative_path: str
    size: int
    sha256: str
    file_version: str | None
    product_version: str | None


@dataclass(frozen=True)
class BuildInspection:
    profile: BuildProfile | None
    fingerprints: tuple[BinaryFingerprint, ...]
    reason: str

    @property
    def supported(self) -> bool:
        return self.profile is not None


@dataclass(frozen=True)
class KnownBuildInspection:
    profile: KnownBuildProfile | None
    fingerprints: tuple[BinaryFingerprint, ...]
    reason: str

    @property
    def known(self) -> bool:
        return self.profile is not None

    @property
    def supported(self) -> bool:
        return bool(
            self.profile is not None and
            self.profile.support_status == "supported"
        )

    @property
    def observed(self) -> bool:
        return bool(
            self.profile is not None and
            self.profile.support_status == "observed-beta"
        )


PC_1_0_0_0_PROFILE = BuildProfile(
    profile_id="fifa19-pc-1.0.0.0-3865658",
    display_name="FIFA 19 PC 1.0.0.0 (19.0.3865658.0)",
    native_adapter_id="cardsdll-19.0.3865658",
    route_adapter_id="fut19-rs4-launch",
    dto_adapter_id="cardsdll-19.0.3865658",
    feature_flags=frozenset({
        "local_redirector",
        "local_blaze",
        "fut_http",
        "native_certificate_compatibility",
        "offline_draft",
        "totw_challenge",
        "squad_battles",
        "local_transfer_market",
        "sbc_runtime",
        "prime_icon_moments_compatibility",
    }),
    executable=BinarySpec(
        relative_path="FIFA19.exe",
        size=292_892_672,
        sha256="da6be40a2edb8761394c9dfff96a5e3ff1e71656deaa1f8056548f0c2e12edd5",
        file_version="1.0.0.0",
        product_version="19.0.3865658.0",
    ),
    modules=(
        BinarySpec(
            relative_path="CardsDLL_Win64_retail.dll",
            size=4_481_856,
            sha256="e0bd4fa1eacc535f265a690147b4304a97df6edbc7dd06c02ee870ceb51c1c2a",
            file_version="1.0.0.0",
            product_version="19.0.3865658.0",
        ),
    ),
    certificate_handler_rva=0x31B1890,
    certificate_success_value=0x15,
)


SUPPORTED_BUILD_PROFILES = (PC_1_0_0_0_PROFILE,)


PC_1_0_0_0_KNOWN_PROFILE = KnownBuildProfile(
    profile_id=PC_1_0_0_0_PROFILE.profile_id,
    display_name=PC_1_0_0_0_PROFILE.display_name,
    support_status="recognized-unvalidated",
    launch_strategy="legacy-native-server",
    selection_priority=100,
    feature_flags=PC_1_0_0_0_PROFILE.feature_flags,
    executable=PC_1_0_0_0_PROFILE.executable,
    modules=PC_1_0_0_0_PROFILE.modules,
)


EA_APP_4052077_OBSERVED_PROFILE = KnownBuildProfile(
    profile_id="fifa19-pc-eaapp-4052077-observed",
    display_name="FIFA 19 PC EA App (19.0.4052077.0)",
    support_status="observed-beta",
    launch_strategy="eaapp-menu-guarded-bridge",
    selection_priority=200,
    feature_flags=frozenset({
        "local_redirector",
        "local_blaze",
        "fut_http",
        "onboarding",
        "fut_hub",
        "local_transfer_market",
    }),
    executable=BinarySpec(
        relative_path="FIFA19.exe",
        size=292_751_680,
        sha256="e03e3b28a8128da382c7c88618171d7e648af88771991d24c8e32722c75f1615",
        file_version="1.0.0.0",
        product_version="19.0.4052077.0",
    ),
    modules=(
        BinarySpec(
            relative_path="CardsDLL_Win64_retail.dll",
            size=4_507_968,
            sha256="8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a",
            file_version="1.0.0.0",
            product_version="19.0.4052077.0",
        ),
    ),
)


KNOWN_BUILD_PROFILES = (
    PC_1_0_0_0_KNOWN_PROFILE,
    EA_APP_4052077_OBSERVED_PROFILE,
)


RUNTIME_ADAPTER_PROFILES = {
    PC_1_0_0_0_PROFILE.profile_id: RuntimeAdapterProfile(
        profile_id=PC_1_0_0_0_PROFILE.profile_id,
        native_adapter_id=PC_1_0_0_0_PROFILE.native_adapter_id,
        route_adapter_id="fut19-v1-3865658-routes",
        dto_adapter_id="cardsdll-v1-3865658-dtos",
        data_namespace="v1-3865658",
        server_mode="legacy-full",
    ),
    EA_APP_4052077_OBSERVED_PROFILE.profile_id: RuntimeAdapterProfile(
        profile_id=EA_APP_4052077_OBSERVED_PROFILE.profile_id,
        native_adapter_id="cardsdll-eaapp-4052077-guarded-bridge",
        route_adapter_id="fut19-eaapp-4052077-routes",
        dto_adapter_id="cardsdll-eaapp-4052077-dtos",
        data_namespace="eaapp-4052077",
        server_mode="network-only",
    ),
}


def runtime_adapter_profile(profile_id: str | None) -> RuntimeAdapterProfile:
    """Resolve all runtime layers without an implicit build default."""
    value = str(profile_id or "").strip()
    if not value:
        raise ValueError("runtime adapter profile is required")
    try:
        return RUNTIME_ADAPTER_PROFILES[value]
    except KeyError as exc:
        raise ValueError(
            "runtime adapter profile is not registered: %s" % value) from exc


class _VSFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


def _quad_version(ms: int, ls: int) -> str:
    return "%d.%d.%d.%d" % (
        (ms >> 16) & 0xFFFF,
        ms & 0xFFFF,
        (ls >> 16) & 0xFFFF,
        ls & 0xFFFF,
    )


def windows_file_versions(path: os.PathLike[str] | str) -> tuple[str | None, str | None]:
    """Read fixed PE file/product versions without starting the binary."""
    if os.name != "nt":
        return None, None
    version = ctypes.WinDLL("version", use_last_error=True)
    get_size = version.GetFileVersionInfoSizeW
    get_size.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    get_size.restype = wintypes.DWORD
    get_info = version.GetFileVersionInfoW
    get_info.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                         wintypes.LPVOID]
    get_info.restype = wintypes.BOOL
    query = version.VerQueryValueW
    query.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                      ctypes.POINTER(wintypes.LPVOID),
                      ctypes.POINTER(wintypes.UINT)]
    query.restype = wintypes.BOOL

    target = os.fspath(path)
    unused = wintypes.DWORD(0)
    size = int(get_size(target, ctypes.byref(unused)))
    if size <= 0:
        return None, None
    buffer = ctypes.create_string_buffer(size)
    if not get_info(target, 0, size, buffer):
        return None, None
    value = wintypes.LPVOID()
    value_size = wintypes.UINT(0)
    if not query(buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)):
        return None, None
    if value_size.value < ctypes.sizeof(_VSFixedFileInfo):
        return None, None
    fixed = ctypes.cast(value, ctypes.POINTER(_VSFixedFileInfo)).contents
    if fixed.dwSignature != 0xFEEF04BD:
        return None, None
    fixed_file = _quad_version(fixed.dwFileVersionMS, fixed.dwFileVersionLS)
    fixed_product = _quad_version(
        fixed.dwProductVersionMS, fixed.dwProductVersionLS)

    # FIFA's fixed product version is 19.0.0.0, while the standard string
    # resource contains the actual build 19.0.3865658.0 shown by Explorer.
    # Prefer those user-visible values when the resource is present.
    translation = wintypes.LPVOID()
    translation_size = wintypes.UINT(0)
    if not query(buffer, "\\VarFileInfo\\Translation",
                 ctypes.byref(translation), ctypes.byref(translation_size)):
        return fixed_file, fixed_product
    if translation_size.value < 4:
        return fixed_file, fixed_product
    words = ctypes.cast(translation, ctypes.POINTER(wintypes.WORD))
    language, codepage = int(words[0]), int(words[1])

    def string_value(name: str, fallback: str) -> str:
        entry = wintypes.LPVOID()
        entry_size = wintypes.UINT(0)
        key = "\\StringFileInfo\\%04x%04x\\%s" % (
            language, codepage, name)
        if not query(buffer, key, ctypes.byref(entry), ctypes.byref(entry_size)):
            return fallback
        if not entry.value or entry_size.value <= 1:
            return fallback
        raw = ctypes.wstring_at(entry.value, entry_size.value - 1).strip()
        return raw.replace(", ", ".").replace(",", ".") or fallback

    return (
        string_value("FileVersion", fixed_file),
        string_value("ProductVersion", fixed_product),
    )


def fingerprint_file(path: os.PathLike[str] | str,
                     relative_path: str | None = None) -> BinaryFingerprint:
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    file_version, product_version = windows_file_versions(target)
    return BinaryFingerprint(
        relative_path=relative_path or target.name,
        size=target.stat().st_size,
        sha256=digest.hexdigest(),
        file_version=file_version,
        product_version=product_version,
    )


def _matches(spec: BinarySpec, actual: BinaryFingerprint) -> bool:
    return (
        spec.relative_path.replace("/", "\\").lower()
        == actual.relative_path.replace("/", "\\").lower()
        and spec.size == actual.size
        and spec.sha256.lower() == actual.sha256.lower()
        and spec.file_version == actual.file_version
        and spec.product_version == actual.product_version
    )


def _inspect_profile_set(
        executable_path: os.PathLike[str] | str,
        profiles: Iterable[BuildProfile | KnownBuildProfile],
        registry_label: str,
) -> tuple[
        BuildProfile | KnownBuildProfile | None,
        tuple[BinaryFingerprint, ...],
        str,
]:
    target = Path(executable_path).resolve()
    profile_list = tuple(profiles)
    try:
        executable = fingerprint_file(target, target.name)
    except (OSError, ValueError) as exc:
        return None, (), "executable fingerprint failed: %s" % exc

    candidates = [profile for profile in profile_list
                  if _matches(profile.executable, executable)]
    if not candidates:
        return (
            None,
            (executable,),
            "FIFA19.exe fingerprint is not in the %s registry" %
            registry_label,
        )

    fingerprints = [executable]
    mismatch_reasons = []
    for profile in candidates:
        current = [executable]
        failed = False
        for spec in profile.modules:
            module_path = target.parent / Path(spec.relative_path)
            try:
                module = fingerprint_file(module_path, spec.relative_path)
            except (OSError, ValueError) as exc:
                mismatch_reasons.append(
                    "%s fingerprint failed: %s" % (spec.relative_path, exc))
                failed = True
                break
            current.append(module)
            if not _matches(spec, module):
                mismatch_reasons.append(
                    "%s fingerprint does not match profile %s" %
                    (spec.relative_path, profile.profile_id))
                failed = True
                break
        if not failed:
            return profile, tuple(current), "exact build match"
        if len(current) > len(fingerprints):
            fingerprints = current

    return None, tuple(fingerprints), "; ".join(mismatch_reasons)


def inspect_installed_build(
        executable_path: os.PathLike[str] | str,
        profiles: Iterable[BuildProfile] = SUPPORTED_BUILD_PROFILES,
) -> BuildInspection:
    """Fingerprint an installation and return only an exact supported match."""
    profile, fingerprints, reason = _inspect_profile_set(
        executable_path, profiles, "supported-build")
    return BuildInspection(profile, fingerprints,
                           "supported build" if profile else reason)


def inspect_known_build(
        executable_path: os.PathLike[str] | str,
        profiles: Iterable[KnownBuildProfile] = KNOWN_BUILD_PROFILES,
) -> KnownBuildInspection:
    """Classify an exact known pair without implying release support."""
    profile, fingerprints, reason = _inspect_profile_set(
        executable_path, profiles, "known-build")
    if profile is not None:
        reason = "%s build" % profile.support_status
    return KnownBuildInspection(profile, fingerprints, reason)


def process_image_path(pid: int) -> str:
    """Resolve a Windows process image without attaching or loading code."""
    if os.name != "nt":
        raise OSError("process image lookup is supported only on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    query_name = kernel32.QueryFullProcessImageNameW
    query_name.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                           ctypes.POINTER(wintypes.DWORD)]
    query_name.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    handle = open_process(process_query_limited_information, False, int(pid))
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not query_name(handle, 0, buffer, ctypes.byref(length)):
            raise OSError(ctypes.get_last_error(),
                          "QueryFullProcessImageNameW failed")
        return buffer.value
    finally:
        close_handle(handle)


def inspect_process_build(pid: int) -> BuildInspection:
    try:
        executable_path = process_image_path(pid)
    except OSError as exc:
        return BuildInspection(None, (), "process image lookup failed: %s" % exc)
    return inspect_installed_build(executable_path)


def fingerprint_summary(
        inspection: BuildInspection | KnownBuildInspection) -> str:
    """Stable diagnostic text without a machine-specific installation path."""
    parts = []
    for item in inspection.fingerprints:
        parts.append("%s size=%d file=%s product=%s sha256=%s" % (
            item.relative_path,
            item.size,
            item.file_version or "unknown",
            item.product_version or "unknown",
            item.sha256,
        ))
    return "; ".join(parts)
