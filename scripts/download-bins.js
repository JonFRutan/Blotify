#!/usr/bin/env node
/**
 * Downloads the platform-appropriate spotdl standalone binary into
 * resources/bin/{platform}/ so electron-builder can include it as
 * an extraResource.
 *
 * Run via:  npm run download-bins
 * Or:       node scripts/download-bins.js [--platform linux|darwin|win32]
 */

'use strict';

const https = require('https');
const http  = require('http');
const fs    = require('fs');
const path  = require('path');

const SPOTDL_VERSION = '4.5.0';

// GitHub release asset URLs (verified against v4.5.0 release)
const SPOTDL_URLS = {
  linux:  `https://github.com/spotDL/spotify-downloader/releases/download/v${SPOTDL_VERSION}/spotdl-${SPOTDL_VERSION}-linux`,
  darwin: `https://github.com/spotDL/spotify-downloader/releases/download/v${SPOTDL_VERSION}/spotdl-${SPOTDL_VERSION}-darwin`,
  win32:  `https://github.com/spotDL/spotify-downloader/releases/download/v${SPOTDL_VERSION}/spotdl-${SPOTDL_VERSION}-win32.exe`,
};

// Parse optional --platform flag
const flagIdx = process.argv.indexOf('--platform');
const targetPlatform = flagIdx !== -1
  ? process.argv[flagIdx + 1]
  : process.platform;

if (!SPOTDL_URLS[targetPlatform]) {
  console.error(`Unsupported platform: ${targetPlatform}. Expected linux | darwin | win32.`);
  process.exit(1);
}

const destDir  = path.join(__dirname, '..', 'resources', 'bin', targetPlatform);
const isWin    = targetPlatform === 'win32';
const destFile = path.join(destDir, isWin ? 'spotdl.exe' : 'spotdl');

fs.mkdirSync(destDir, { recursive: true });

// ── Download with redirect following ──────────────────────────────────────

function download(url, dest, label) {
  return new Promise((resolve, reject) => {
    const tmp = dest + '.tmp';
    const file = fs.createWriteStream(tmp);
    let received = 0;
    let total = 0;

    const get = (url) => {
      const mod = url.startsWith('https') ? https : http;
      mod.get(url, { headers: { 'User-Agent': 'Blotify-builder/1.0' } }, (res) => {
        if (res.statusCode === 301 || res.statusCode === 302) {
          res.resume();
          return get(res.headers.location);
        }
        if (res.statusCode !== 200) {
          file.close();
          fs.unlinkSync(tmp);
          return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
        }

        total = parseInt(res.headers['content-length'] || '0', 10);
        res.on('data', chunk => {
          received += chunk.length;
          if (total) {
            const pct = Math.round((received / total) * 100);
            process.stdout.write(`\r  ${label}: ${pct}% (${(received / 1_048_576).toFixed(1)} MB)`);
          }
        });
        res.pipe(file);
        file.on('finish', () => {
          file.close(() => {
            process.stdout.write('\n');
            fs.renameSync(tmp, dest);
            resolve();
          });
        });
      }).on('error', err => {
        file.close();
        if (fs.existsSync(tmp)) fs.unlinkSync(tmp);
        reject(err);
      });
    };

    get(url);
  });
}

// ── Main ───────────────────────────────────────────────────────────────────

(async () => {
  const url = SPOTDL_URLS[targetPlatform];
  const name = path.basename(destFile);

  if (fs.existsSync(destFile)) {
    console.log(`✓ ${name} already exists — skipping (delete to re-download).`);
  } else {
    console.log(`↓ Downloading spotdl v${SPOTDL_VERSION} for ${targetPlatform}…`);
    await download(url, destFile, name);
    // Make executable on Unix
    if (!isWin) fs.chmodSync(destFile, 0o755);
    console.log(`✓ Saved to ${destFile}`);
  }

  console.log('\nNote: The server binary (resources/bin/{platform}/server) must be');
  console.log('      built with PyInstaller before packaging — run: npm run build-server');
})().catch(err => {
  console.error('\n✗ Download failed:', err.message);
  process.exit(1);
});
