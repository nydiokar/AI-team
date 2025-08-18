---
id: example_001
type: fix
priority: high
created: 2025-08-03T16:30:00Z
---

# Task: Fix Database Connection Issues

**Target Files:**
- /app/database.py
- /config/db_config.json

**Prompt:**
Analyze the database connection code and fix any timeout or connection pooling issues. Focus on error handling and retry logic.

**Success Criteria:**
- [ ] Connection timeouts handled gracefully
- [ ] Connection pooling configured properly  
- [ ] Error logging improved
- [ ] Unit tests updated

**Context:**
Previous attempts failed due to connection pool exhaustion during high load.