# AI Task Orchestrator: Complete Implementation Plan

## Architecture Overview

```
You (Telegram) → LLAMA (Intelligent Mediator) → Claude Code (Remote Executor) → Results → LLAMA → You
```

**Key Insight: LLAMA is the "brain" that mediates between user intent and Claude's execution**

### LLAMA's Mediation Role
1. **Pre-emptive Problem Solving**: Craft prompts that avoid interactive scenarios
2. **Interactive Prompt Resolution**: When Claude hits prompts, LLAMA can suggest alternatives
3. **Claude Confusion Handling**: If Claude doesn't understand, LLAMA rephrases or tries different approach
4. **Context Management**: Maintains conversation flow and task understanding

### Interactive Prompt Strategy
- **Primary**: Use proper CLI flags (`--dangerously-skip-permissions`, `-p`) to prevent prompts
- **Secondary**: LLAMA detects when Claude gets stuck and mediates
- **Safety Net**: Interactive detection as fallback, but not primary solution
- **False Positive Protection**: Only fail if task is genuinely incomplete, not just on prompt detection
## Phase 1: Foundation (Week 1-2)

### Core Components
1. File Watcher System
2. Task Parser 
3. Claude Code Bridge
4. Basic Validation Layer

### Directory Structure
```
/orchestrator/
├── tasks/           # .task.md files
├── results/         # Claude outputs
├── summaries/       # LLAMA summaries
├── logs/           # System logs
└── config/         # Settings
```
### Task File Format
```yaml
---
id: task_001
type: code_review|summarize|fix|analyze
priority: high|medium|low
created: 2025-08-03T10:30:00Z
---
```

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
## Phase 2: Claude Integration (Week 2-4)

### Claude Code Automation Solutions

#### Option A: CLI Flags (Check First)
```bash
# Test these commands:
claude --help
claude --auto-approve --help
claude --yes --help
```
#### Option B: Input Automation
```python
class ClaudeCodeBridge:
    def init(self):
        self.base_command = [
            'claude', 
            '--dangerously-skip-permissions',  # No confirmations!
            '--output-format', 'json',         # Structured output
            '-p'                               # Headless mode
        ]
    
    def execute_task(self, prompt, allowed_tools=None):
        command = self.base_command.copy()
        
        if allowed_tools:
            # Use safer approach with specific tool permissions
            command = [
                'claude',
                '--allowedTools'] + allowed_tools + [
                '--output-format', 'json',
                '-p'
            ]
        
        command.append(prompt)
        
        result = subprocess.run(
            command, 
            capture_output=True, 
            text=True,
            cwd=your_project_directory
        )
        
        return {
            'output': result.stdout,
            'errors': result.stderr,
            'success': result.returncode == 0,
            'parsed': json.loads(result.stdout) if result.stdout else None
        }
```
#### Option C: API Fallback - NO NEED, we documentations shows that actual claude code will do th work.
```python
class AnthropicAPIBridge:
    def __init__(self, api_key):
        self.client = anthropic.Anthropic(api_key=api_key)
    
    def execute_task(self, prompt, file_paths):
        # Read files and construct prompt
        file_contents = {}
        for path in file_paths:
            with open(path, 'r') as f:
                file_contents[path] = f.read()
        
        full_prompt = f"""
        {prompt}
        
        Files to analyze:
        {self._format_files(file_contents)}
        
        Provide specific code changes and explanations.
        """
        
        response = self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=4000,
            messages=[{"role": "user", "content": full_prompt}]
        )
        
        return response.content[0].text
```
## Phase 3: LLAMA Integration (Week 3-5)

### Local LLM Bridge - Intelligent Mediator
```python
import ollama

class LLAMAMediator:
    def __init__(self):
        self.client = ollama.Client()
        self.model = "llama3.1:8b"  # or gemma2:9b
        self.context_history = []
    
    def parse_task(self, task_file_path):
        """Parse .task.md file into structured data"""
        with open(task_file_path, 'r') as f:
            content = f.read()
        
        prompt = f"""
        Parse this task file and extract:
        1. Task type (code_review, summarize, fix, analyze)
        2. Target files (list)
        3. Main prompt/request
        4. Priority level
        
        Task file:
        {content}
        
        Respond in JSON format only.
        """

        response = self.client.generate(
            model=self.model,
            prompt=prompt,
            format='json'
        )
        
        return json.loads(response['response'])
    
    def create_claude_prompt(self, parsed_task):
        """Convert structured task to Claude-optimized prompt"""
        return f"""
        Task: {parsed_task['type'].upper()}
        
        Focus Areas: {parsed_task['main_request']}
        
        Files to examine: {', '.join(parsed_task['target_files'])}
        
        Please provide:
        1. Summary of current state
        2. Specific issues found
        3. Recommended changes
        4. Code examples where applicable
        
        Format your response with clear sections and actionable items.
        """
    
    def handle_claude_confusion(self, claude_output, original_task, error_type):
        """LLAMA mediates when Claude gets confused or hits interactive prompts"""
        
        if error_type == "interactive_prompt":
            # Claude hit a trust prompt - LLAMA suggests non-interactive approach
            return self._suggest_alternative_approach(original_task, "trust_prompt")
        
        elif error_type == "unclear_output":
            # Claude's output is confusing - LLAMA rephrases the request
            return self._rephrase_for_clarity(original_task, claude_output)
        
        elif error_type == "incomplete_task":
            # Task seems incomplete - LLAMA determines if it's actually done
            return self._assess_completion(original_task, claude_output)
        
        return None  # No mediation needed
    
    def _suggest_alternative_approach(self, task, issue_type):
        """LLAMA suggests different ways to accomplish the task"""
        if issue_type == "trust_prompt":
            return f"""
            Task: {task['main_request']}
            
            Alternative approach to avoid trust prompts:
            1. Use read-only analysis first
            2. Generate code changes as suggestions
            3. Provide manual steps for user to execute
            
            This avoids the need for Claude to modify files directly.
            """
    
    def _rephrase_for_clarity(self, task, claude_output):
        """LLAMA rephrases the task to be clearer for Claude"""
        return f"""
        Rephrased task for clarity:
        
        Instead of: {task['main_request']}
        
        Try: "Analyze the code structure and provide specific, actionable recommendations 
        with code examples. Focus on {task['main_request']}"
        
        Claude output was: {claude_output[:200]}...
        """
    
    def _assess_completion(self, task, claude_output):
        """LLAMA determines if task is actually complete despite seeming incomplete"""
        # Analyze if the core request was fulfilled
        # Check if output contains actionable information
        # Determine if "incomplete" is just a false positive
        pass
    
    def summarize_claude_result(self, claude_output, original_task):
        """Create concise summary for user notification"""
        prompt = f"""
        Summarize this Claude Code result for a busy developer:
        
        Original task: {original_task['main_request']}
        Claude result: {claude_output}
        
        Provide:
        1. What was accomplished (1-2 sentences)
        2. Key findings (max 3 bullet points)
        3. Next steps needed (if any)
        4. Status: SUCCESS/PARTIAL/FAILED
        
        Keep it under 200 words, focus on actionable information.
        """
        
        response = self.client.generate(
            model=self.model,
            prompt=prompt
        )
        
        return response['response']
```

## Phase 4: Validation & Reliability (Week 4-6)

### Validation Layer
```python
class ValidationEngine:
    def __init__(self):
        self.similarity_threshold = 0.7
        self.entropy_threshold = 0.8
    
    def validate_llama_output(self, input_text, llama_output, task_type):
        """Validate LLAMA's interpretation isn't hallucinating"""
        
        # 1. Similarity check
        similarity = self._calculate_similarity(input_text, llama_output)
        
        # 2. Entropy check (randomness detection)
        entropy = self._calculate_entropy(llama_output)
        
        # 3. Structure validation
        structure_valid = self._validate_structure(llama_output, task_type)
        
        return {
            'valid': similarity > self.similarity_threshold and 
                    entropy < self.entropy_threshold and 
                    structure_valid,
            'similarity': similarity,
            'entropy': entropy,
            'issues': self._identify_issues(similarity, entropy, structure_valid)
        }
```
    
    def _calculate_similarity(self, text1, text2):
        """Use sentence transformers or similar"""
        # Implementation with sentence-transformers library
        pass
    
    def _calculate_entropy(self, text):
        """Measure randomness/coherence"""
        # Shannon entropy calculation
        pass
    
    def validate_claude_result(self, result, expected_files):
        """Validate Claude actually did what was requested"""
        return {
            'files_modified': self._check_modified_files(expected_files),
            'output_coherent': self._check_output_coherence(result),
            'errors_present': self._scan_for_errors(result)
        }

### Error Recovery
```python
class ErrorRecovery:
    def __init__(self):
        self.max_retries = 3
        self.backoff_multiplier = 2
    
    def retry_with_backoff(self, func, *args, **kwargs):
        """Exponential backoff for failed operations"""
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(self.backoff_multiplier ** attempt)

    def recover_from_claude_failure(self, task, error):
        """Fallback strategies when Claude fails"""
        if "rate limit" in str(error).lower():
            return self._handle_rate_limit(task)
        elif "timeout" in str(error).lower():
            return self._handle_timeout(task)
        else:
            return self._fallback_to_local_processing(task)
```

## Phase 5: Telegram Integration (Week 5-6)

### Telegram Bot
```python
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler

class TelegramInterface:
    def __init__(self, bot_token, orchestrator):
        self.bot_token = bot_token
        self.orchestrator = orchestrator
        self.app = Application.builder().token(bot_token).build()
        self._setup_handlers()
    
    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("task", self.handle_task_command))
        self.app.add_handler(CommandHandler("status", self.handle_status_command))
        self.app.add_handler(MessageHandler(filters.TEXT, self.handle_message))
    
    async def handle_task_command(self, update: Update, context):
        """Handle /task command"""
        task_description = ' '.join(context.args)
        
        # Create task file
        task_id = self.orchestrator.create_task_from_description(task_description)
        
        await update.message.reply_text(
            f"✅ Task created: {task_id}\n"
            f"📝 Description: {task_description}\n"
            f"⏳ Processing will begin shortly..."
        )
    
    async def notify_completion(self, task_id, summary, success=True):
        """Notify user when task completes"""
        icon = "✅" if success else "❌"
        message = f"{icon} Task {task_id} completed\n\n{summary}"
        
        # Send to registered users
        await self.send_to_users(message)
    
    async def handle_message(self, update: Update, context):
        """Handle natural language task requests"""
        message = update.message.text
        
        # Let LLAMA interpret the message as a task
        task_interpretation = self.orchestrator.interpret_message(message)
        
        if task_interpretation['is_task']:
            task_id = self.orchestrator.create_task(task_interpretation)
            await update.message.reply_text(
                f"📋 Understood: {task_interpretation['summary']}\n"
                f"🔄 Processing as task {task_id}..."
            )
        else:
            await update.message.reply_text(
                "🤔 I'm not sure what task you'd like me to perform. "
                "Try being more specific or use /task <description>"
            )
```

## Phase 6: System Integration (Week 6-8)

### Main Orchestrator
```python
class TaskOrchestrator:
    def __init__(self):
        self.file_watcher = FileWatcher('tasks/')
        self.llama = LLAMAMediator()
        self.claude_bridge = ClaudeCodeBridge()
        self.validator = ValidationEngine()
        self.telegram = TelegramInterface(BOT_TOKEN, self)
        self.task_queue = asyncio.Queue()
        
    async def start(self):
        """Start all components"""
        # Start file watcher
        self.file_watcher.start(callback=self.handle_new_task)
        
        # Start Telegram bot
        await self.telegram.start()
        
        # Start task processor
        asyncio.create_task(self.process_task_queue())
        
        print("🚀 AI Task Orchestrator started")
    
    async def handle_new_task(self, task_file_path):
        """Process new .task.md file"""
        try:
            # 1. Parse with LLAMA
            parsed_task = self.llama.parse_task(task_file_path)
            
            # 2. Validate parsing
            validation = self.validator.validate_llama_output(

                open(task_file_path).read(), 
                str(parsed_task), 
                parsed_task.get('type')
            )
            
            if not validation['valid']:
                await self.telegram.notify_error(
                    f"❌ Task parsing failed: {validation['issues']}"
                )
                return
            
            # 3. Create Claude prompt
            claude_prompt = self.llama.create_claude_prompt(parsed_task)
            
            # 4. Execute with Claude
            claude_result = await self.claude_bridge.execute_with_auto_confirm(
                claude_prompt, 
                parsed_task['target_files']
            )
            
            # 5. Validate Claude result
            result_validation = self.validator.validate_claude_result(
                claude_result, 
                parsed_task['target_files']
            )
            
            # 6. Summarize with LLAMA
            summary = self.llama.summarize_claude_result(
                claude_result['output'], 
                parsed_task
            )
            
            # 7. Notify user
            await self.telegram.notify_completion(
                parsed_task['id'], 
                summary, 
                claude_result['success']
            )
            
        except Exception as e:
            await self.telegram.notify_error(f"💥 Task failed: {str(e)}")
            logging.error(f"Task processing error: {e}", exc_info=True)
```

## Interactive Reply Flow (Turn-Based, Mediated)

### Objective
Allow users to reply to an already-processed task (e.g., “yes, proceed but skip A/B and focus on X”) while preserving context. LLAMA mediates the constraints and re-invokes Claude in a new non-interactive turn.

### Flow
1. Turn 0: Initial task → LLAMA parses/frames → Claude executes (headless) → artifact v1 written
2. User reply: via Telegram `/reply <task_id> ...`
3. LLAMA mediation: summarize prior context, apply constraints, craft next prompt
4. Turn 1: Re-invoke Claude with prior context and constraints (still headless)
5. Persist results: append to `conversation` in artifact, or create a linked artifact
6. Emit events: `turn_started`, `turn_finished` referencing the original task_id and turn index

### Data Model Changes
- Artifact adds optional `conversation: [{ role: "user"|"llama"|"claude", content: string, timestamp: iso, turn: int }]`
- Turn linkage via `parent_task_id` or `turn_of` for derived artifacts

### Prerequisites
- Artifact schema v1 (with `schema_version`, status blocks) and validator
- CLI hardening (non-interactive flags, retry taxonomy, triage)
- Minimal context loader (reads previous artifact for summarization)

### Acceptance
- A reply produces a new turn that respects constraints and validates against schema
- Conversation is visible in artifacts and summaries, with clear turn boundaries
- No reliance on interactive CLI prompts; all turns are headless

### Activation with Current Artifacts and Processed Tasks

- Current behavior: completed tasks are archived to `tasks/processed/` and results are in `results/<task_id>.json`
- We will rely on artifacts (not the original `.task.md`) as the source of truth for replies
- Required enhancements to activate:
  1. Schema: optional `conversation` block; `parent_task_id`/`turn_of` linkage for derived turns
  2. Artifact index: simple lookup to find latest artifact for a `task_id`
  3. Context loader: read last artifact → build condensed context for LLAMA (summary, constraints, files_modified)
  4. Telegram `/reply <task_id> ...`: authorized, rate-limited endpoint that enqueues a new turn
  5. Orchestrator `turn` execution: LLAMA mediates constraints → Claude executes headless → new-turn artifact written
  6. Events: `turn_started`, `turn_finished` including `parent_task_id` and `turn_index`
- Non-goals:
  - No persistent live CLI session; each turn is a stateless, idempotent invocation
  - No schema-breaking changes; all new fields are optional and versioned

#### Remediation checklist (do-first tasks, non-breaking)
- Config
  - Add `reload_from_env()` or a `doctor` CLI command to print/verify effective config (timeout, max_turns, flags)
  - Document expectation that env should be set before process start if hot-reload is not implemented
- Artifact index/loader
  - Implement `results.index.json` (map task_id → latest artifact path, updated on write)
  - Add `load_context(task_id)` in orchestrator to produce a compact context (summary, constraints, files list)
- Events
  - Emit `turn_started`/`turn_finished` with `parent_task_id`, `turn_index` and worker id
  - Extend stats tooling to display turn counts per task
- Schema/validator
  - Add optional `conversation` and `parent_task_id`/`turn_of` fields; keep strict default; provide `--ignore-legacy`
- Tests
  - Unit: config env overrides; artifact index; context loader; turn events; schema for new fields
  - E2E: reply flow creates a new turn that respects constraints and validates strictly

## Critical Success Factors

### 1. Claude Code Automation (Must Solve First)
- Test all CLI automation approaches
- Have API fallback ready
- Document exact confirmation patterns

### 2. LLAMA Output Reliability
- Implement structured prompting with examples
- Use JSON mode when available
- Build comprehensive validation

### 3. File System Robustness
- Handle race conditions with file locking
- Implement atomic file operations
- Add comprehensive error recovery

### 4. Context Management
- Keep LLAMA context focused and fresh
- Implement context rotation/cleanup
- Monitor context drift over time

## Monitoring & Debugging

### Key Metrics to Track
- Task success rate
- LLAMA hallucination frequency
- Claude Code failure patterns
- Average task completion time
- System uptime

### Debug Tools
- Structured logging at each step
- Task state visualization
- Context history browser
- Performance profiling

## Deployment Strategy

### Development Environment
1. Local Ollama with Llama 3.1 8B
2. Claude Code CLI tool
3. File system monitoring
4. Telegram bot (test token)

### Production Considerations
- Process supervision (systemd/supervisor)
- Log rotation and monitoring
- Backup strategies for task history
- Resource usage monitoring

## Risk Mitigation

### High-Risk Items
1. Claude Code CLI changes → Maintain API fallback
2. LLAMA hallucination → Strong validation layer
3. File system race conditions → Atomic operations + locking
4. Context drift → Regular context cleanup

### Medium-Risk Items
1. Telegram API limits → Rate limiting + queuing
2. Local compute resources → Resource monitoring
3. Task queue overflow → Queue size limits + prioritization

## Success Criteria

### Minimum Viable Product (8 weeks)
- [ ] File-based task creation works
- [ ] LLAMA can parse and route tasks
- [ ] Claude Code integration automated (80% success rate)
- [ ] Basic Telegram notifications
- [ ] Simple validation and error handling

### Full Feature Set (12 weeks)
- [ ] Complex multi-step task workflows
- [ ] Robust error recovery
- [ ] Advanced validation and monitoring
- [ ] Rich Telegram interface with status queries
- [ ] Performance optimization and scaling

This plan addresses your core concerns while maintaining realistic expectations about complexity and timeline.