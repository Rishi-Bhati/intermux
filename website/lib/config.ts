export const GITHUB_URL = "https://github.com/Rishi-Bhati/intermux";
export const DOCS_URL = "https://github.com/Rishi-Bhati/intermux#readme";
export const RELEASES_URL = `${GITHUB_URL}/releases`;
export const LATEST_RELEASE_URL = `${GITHUB_URL}/releases/latest`;
export const WINDOWS_URL = `${GITHUB_URL}/blob/main/windows/README.md`;
export const VERSION = "2.0.1";

/** The asset every Linux release carries. `releases/latest/download/<name>` always points at the newest one. */
export const TARBALL = "intermux-linux-x86_64.tar.gz";
export const TARBALL_URL = `${GITHUB_URL}/releases/latest/download/${TARBALL}`;

/** The one-line installers live in the main repo under get/ and are served from GitHub. */
export const RAW_GET = "https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get";
export const INSTALLER_SOURCE_URL = `${GITHUB_URL}/tree/main/get`;

/**
 * Where the install commands point. Once the site has its own domain (SITE_URL), next.config.ts
 * redirects /install and /install.ps1 to the scripts, so the commands get short; until then they
 * use the raw GitHub URLs, which work today.
 */
const SITE = process.env.SITE_URL?.replace(/\/+$/, "");
export const SH_URL = SITE ? `${SITE}/install` : `${RAW_GET}/install.sh`;
export const PS1_URL = SITE ? `${SITE}/install.ps1` : `${RAW_GET}/install.ps1`;

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
