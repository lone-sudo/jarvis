-- 003_network_allowlist: V3. Deny-by-default -- starts empty. A domain
-- is fetchable only after `jarvis network-allow <domain>` adds it here.

CREATE TABLE IF NOT EXISTS network_allowlist (
    domain TEXT PRIMARY KEY,            -- normalized: lowercase, no trailing dot
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
