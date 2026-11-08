export const GITHUB_URL = "https://github.com/Rishi-Bhati/intermux";
export const DOCS_URL = "/docs";
export const RELEASES_URL = `${GITHUB_URL}/releases`;
export const LATEST_RELEASE_URL = `${GITHUB_URL}/releases/latest`;
export const WINDOWS_URL = `${GITHUB_URL}/blob/main/windows/README.md`;
export const VERSION = "2.0.1";

/** The asset every Linux release carries. `releases/latest/download/<name>` always points at the newest one. */
export const TARBALL = "intermux-linux-x86_64.tar.gz";
export const TARBALL_URL = `${GITHUB_URL}/releases/latest/download/${TARBALL}`;

/** The one-line installers live in the main repo under get/. */
export const RAW_GET = "https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get";
export const INSTALLER_SOURCE_URL = `${GITHUB_URL}/tree/main/get`;

/**
 * The public site. Override with SITE_URL for a preview or a different domain.
 * Install commands point here; next.config.ts redirects them to the scripts in get/.
 */
const configuredSite = process.env.SITE_URL ?? "https://intermux.me";
export const SITE_URL = (configuredSite.includes("://") ? configuredSite : `https://${configuredSite}`).replace(/\/+$/, "");

export const SH_URL = `${SITE_URL}/install.sh`;
export const PS1_URL = `${SITE_URL}/install.ps1`;

export const INSTALL_COMMAND = `curl -fsSL ${SH_URL} | sh`;
export const WGET_COMMAND = `wget -qO- ${SH_URL} | sh`;
export const WINDOWS_COMMAND = `irm ${PS1_URL} | iex`;

/** Flags the Linux installer takes after `sh -s --`. */
export const INSTALL_OPTIONS = [
  { flag: "--version X.Y.Z", desc: "Install a specific release instead of the newest" },
  { flag: "--from-source", desc: "Build from source instead of using the pre-built bundle" },
  { flag: "--no-deps", desc: "Do not install missing system packages" },
  { flag: "--uninstall", desc: "Reset every binding and remove InterMux" },
];

/** The checked install of the pre-built bundle, as the README describes it. */
export const BUNDLE_STEPS = [
  `curl -LO ${TARBALL_URL}`,
  `curl -LO ${TARBALL_URL}.sha256`,
  `sha256sum -c ${TARBALL}.sha256`,
  `sudo tar -xzf ${TARBALL}`,
  "cd intermux-linux && sudo ./install.sh",
];

export const SOURCE_STEPS = [
  "git clone https://github.com/Rishi-Bhati/intermux.git",
  "cd intermux",
  "sudo ./install.sh",
];
