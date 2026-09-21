const path = require('node:path');
const stage = process.env.RECRUITOPS_DESKTOP_PACKAGE_STAGE;
module.exports = {
  packagerConfig: {
    asar: true,
    executableName: 'RecruitOps-Desktop-Preview',
    extraResource: stage ? ['desktop-runtime','desktop-browser','desktop-filler','desktop-bootstrap.py'].map(name=>path.join(stage,name)) : [],
    ignore: [/^\/tests($|\/)/, /^\/test-results($|\/)/, /^\/\.cache($|\/)/, /^\/\.package-staging($|\/)/, /^\/out($|\/)/]
  },
  hooks: { generateAssets: async () => { if (!stage) throw new Error('Use npm run package with RECRUITOPS_DESKTOP_PACKAGE_RUNTIME; raw Forge packaging is disabled'); } },
  makers: []
};
