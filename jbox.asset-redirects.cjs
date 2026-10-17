const fs = require('node:fs');
const path = require('node:path');
module.exports = function withJboxAssets(config) {
  if (typeof config === 'function') return async (...args) => withJboxAssets(await config(...args));
  const previous = config.redirects;
  const manifest = JSON.parse(fs.readFileSync(path.join(process.cwd(), 'jbox.assets.json'), 'utf8'));
  const mappings = Object.entries(manifest.legacyPaths || {}).filter(([source]) => source.startsWith('/'));
  return { ...config, images: { ...config.images, unoptimized: true },
    async redirects() {
      const redirects = mappings.map(([source, asset]) => ({ source: source.replace(/[:*+?(){}\[\]]/g, '\\$&'), destination: asset.assetUrl, permanent: false }));
      return [...redirects, ...(previous ? await previous() : [])];
    }
  };
};
