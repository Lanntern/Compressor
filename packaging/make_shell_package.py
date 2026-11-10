"""Lay out the sparse package that puts Compressor in Windows 11's main right-click menu (not only under "Show
more options"): an AppxManifest.xml plus the logos Windows asks for. CI packs it with makeappx into
shell/CompressorShell.msix next to shell/CompressorShell.dll (packaging/shell/CompressorShell.cpp).

"Sparse" means the package holds no app files: it gives the installed app (its folder is the package's external
location) an identity, which is what lets Explorer load the menu handler. It's unsigned: Windows 11 accepts
that for packages whose publisher carries the "unsigned" OID below, installed with Add-AppxPackage
-AllowUnsigned (compressor/gui/integration.py does it per user). Nothing here is needed on Windows 10, whose
menu has no such split.

Usage: python packaging/make_shell_package.py OUT_DIR
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from compressor import APP_NAME, DESCRIPTION, __version__  # noqa: E402

PACKAGE_NAME = "Lanntern.Compressor.ContextMenu"        # (also compressor/gui/integration.py)
# The publisher of an unsigned package must include this OID (Microsoft's rule for -AllowUnsigned).
PUBLISHER = "CN=Lanntern, OID.2.25.311729368913984317654407730594956997722=1"
CLSID_COMPRESS = "c0dc2a7f-2eb1-46b8-8cd3-549d97989547"  # (also CompressorShell.cpp)
CLSID_CONVERT = "ab141279-6699-40e3-9afc-ae9f9f4373dc"


def package_version(version: str = __version__) -> str:
    parts = [p for p in version.split(".") if p.isdigit()][:3]
    return ".".join(parts + ["0"] * (4 - len(parts)))


def manifest(version: str = __version__) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
         xmlns:uap10="http://schemas.microsoft.com/appx/manifest/uap/windows10/10"
         xmlns:desktop4="http://schemas.microsoft.com/appx/manifest/desktop/windows10/4"
         xmlns:desktop5="http://schemas.microsoft.com/appx/manifest/desktop/windows10/5"
         xmlns:com="http://schemas.microsoft.com/appx/manifest/com/windows10"
         xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
         IgnorableNamespaces="uap uap10 desktop4 desktop5 com rescap">
  <Identity Name="{PACKAGE_NAME}" Publisher="{PUBLISHER}" Version="{package_version(version)}"
            ProcessorArchitecture="x64"/>
  <Properties>
    <DisplayName>{APP_NAME}</DisplayName>
    <PublisherDisplayName>Lanntern</PublisherDisplayName>
    <Logo>Assets\\StoreLogo.png</Logo>
    <uap10:AllowExternalContent>true</uap10:AllowExternalContent>
  </Properties>
  <Resources>
    <Resource Language="en-us"/>
  </Resources>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.22000.0" MaxVersionTested="10.0.26100.0"/>
  </Dependencies>
  <Capabilities>
    <rescap:Capability Name="runFullTrust"/>
    <rescap:Capability Name="unvirtualizedResources"/>
  </Capabilities>
  <Applications>
    <Application Id="{APP_NAME}" Executable="{APP_NAME}.exe" uap10:TrustLevel="mediumIL"
                 uap10:RuntimeBehavior="win32App">
      <uap:VisualElements AppListEntry="none" DisplayName="{APP_NAME}" Description="{DESCRIPTION}"
                          BackgroundColor="transparent" Square150x150Logo="Assets\\Square150x150Logo.png"
                          Square44x44Logo="Assets\\Square44x44Logo.png"/>
      <Extensions>
        <desktop4:Extension Category="windows.fileExplorerContextMenus">
          <desktop4:FileExplorerContextMenus>
            <desktop5:ItemType Type="*">
              <desktop5:Verb Id="CompressorCompress" Clsid="{CLSID_COMPRESS}"/>
              <desktop5:Verb Id="CompressorConvert" Clsid="{CLSID_CONVERT}"/>
            </desktop5:ItemType>
            <desktop5:ItemType Type="Directory">
              <desktop5:Verb Id="CompressorCompressFolder" Clsid="{CLSID_COMPRESS}"/>
            </desktop5:ItemType>
          </desktop4:FileExplorerContextMenus>
        </desktop4:Extension>
        <com:Extension Category="windows.comServer">
          <com:ComServer>
            <com:SurrogateServer DisplayName="{APP_NAME} menu">
              <com:Class Id="{CLSID_COMPRESS}" Path="shell\\CompressorShell.dll" ThreadingModel="STA"/>
              <com:Class Id="{CLSID_CONVERT}" Path="shell\\CompressorShell.dll" ThreadingModel="STA"/>
            </com:SurrogateServer>
          </com:ComServer>
        </com:Extension>
      </Extensions>
    </Application>
  </Applications>
</Package>
"""


def main(out: Path) -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication([])  # noqa: F841 - the icon is drawn with Qt
    from compressor.gui.main_window import app_icon

    (out / "Assets").mkdir(parents=True, exist_ok=True)
    (out / "AppxManifest.xml").write_text(manifest(), encoding="utf-8")
    for name, size in (("StoreLogo", 50), ("Square150x150Logo", 150), ("Square44x44Logo", 44)):
        app_icon().pixmap(size, size).save(str(out / "Assets" / f"{name}.png"))
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "shell-package")))
