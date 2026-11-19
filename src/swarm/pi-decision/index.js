"use strict";

// Managed sessions load decision.js through the trusted mailbox entry point.
// This standalone package must not register the same tool a second time there.
module.exports = function decisionPackage(pi) {
  if (process.env.SWARM_CONTROL_DIR && process.env.SWARM_AGENT_ID) return;
  return require('../extensions/decision.js').default(pi);
};
module.exports.default = module.exports;
