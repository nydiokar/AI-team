# Codex Integration Technical Document

## Overview

This document outlines the technical approach for replacing Claude Code CLI with OpenAI Codex in the AI-Team project orchestrator. The AI-Team project currently uses Claude as the primary AI agent for task execution, with LLAMA serving as a mediator for parsing and optimization.

## Current Architecture Analysis

### Key Components Using Claude

1. **ClaudeBridge** (`src/bridges/claude_bridge.py`)
   - Primary interface for executing tasks
   - Manages CLI command construction and execution
   - Handles task result parsing and error classification
   - Tool permission management

2. **Configuration** (`config/settings.py`)
   - ClaudeConfig class with CLI parameters
   - Command construction and timeout management
   - Working directory and security controls

3. **Task Orchestrator** (`src/orchestrator.py`)
   - Main coordination between ClaudeBridge and LlamaMediator
   - Task lifecycle management and result processing

## Technical Implementation Plan

### Phase 1: Codex Bridge Implementation

#### 1.1 Create CodexBridge Class

Replace `ClaudeBridge` with a new `CodexBridge` class:

```python
# Location: src/bridges/codex_bridge.py
from openai import OpenAI
import json
import time
from typing import Dict, List, Any, Optional
from src.core import IClaudeBridge, Task, TaskResult, TaskStatus, TaskType
from config import config

class CodexBridge(IClaudeBridge):
    """Bridge to interact with OpenAI Codex API"""
    
    def __init__(self):
        self.client = OpenAI(
            api_key=config.codex.api_key,
            base_url=config.codex.base_url or "https://api.openai.com/v1"
        )
        self.model = config.codex.model
    
    async def execute_task(self, task: Task) -> TaskResult:
        """Execute a task using Codex API"""
        # Implementation details below
        
    def test_connection(self) -> bool:
        """Test if Codex API is available and working"""
        # Implementation details below
```

#### 1.2 API Integration Strategy

**Authentication & Configuration:**
- Add OpenAI API key management to environment configuration
- Implement rate limiting and retry logic for API calls
- Add model selection (codex-code-davinci-002, gpt-4-turbo, etc.)

**Request Structure:**
```python
def _build_codex_request(self, task: Task) -> Dict[str, Any]:
    """Build Codex API request from task"""
    return {
        "model": self.model,
        "messages": [
            {
                "role": "system",
                "content": self._get_system_prompt(task.type)
            },
            {
                "role": "user", 
                "content": self._build_task_prompt(task)
            }
        ],
        "max_tokens": config.codex.max_tokens,
        "temperature": config.codex.temperature,
        "tools": self._get_codex_tools(task.type) if config.codex.use_tools else None
    }
```

### Phase 2: Configuration Updates

#### 2.1 Replace ClaudeConfig with CodexConfig

```python
# Location: config/settings.py
@dataclass
class CodexConfig:
    """OpenAI Codex API configuration"""
    api_key: str
    model: str = "gpt-4-turbo"  # or codex-code-davinci-002 if available
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.1
    timeout: int = 300
    use_tools: bool = True
    max_retries: int = 3
    retry_delay: float = 1.0
    
    # Rate limiting
    requests_per_minute: int = 20
    tokens_per_minute: int = 40000
    
    # Working directory controls (preserved from Claude)
    base_cwd: Optional[str] = None
    allowed_root: Optional[str] = None
```

#### 2.2 Environment Variables

Update `.env` configuration:
```bash
# Replace Claude environment variables
OPENAI_API_KEY=your_openai_api_key_here
CODEX_MODEL=gpt-4-turbo
CODEX_MAX_TOKENS=4096
CODEX_TEMPERATURE=0.1
CODEX_BASE_URL=https://api.openai.com/v1

# Preserve existing working directory controls
CODEX_BASE_CWD=C:\Users\Cicada38\Projects
CODEX_ALLOWED_ROOT=C:\Users\Cicada38\Projects
```

### Phase 3: Tool Integration

#### 3.1 Function Calling Implementation

Convert Claude's tool system to OpenAI function calling:

```python
def _get_codex_tools(self, task_type) -> List[Dict[str, Any]]:
    """Convert task-based tools to OpenAI function definitions"""
    base_tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read contents of a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to file to read"}
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function", 
            "function": {
                "name": "write_file",
                "description": "Write content to a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to file to write"},
                        "content": {"type": "string", "description": "Content to write"}
                    },
                    "required": ["file_path", "content"]
                }
            }
        }
        # Add more tools as needed
    ]
    
    # Filter tools based on task type and security settings
    return self._filter_tools_by_task_type(base_tools, task_type)
```

#### 3.2 Tool Execution Framework

Implement local tool execution:

```python
class CodexToolExecutor:
    """Execute function calls locally with security controls"""
    
    def __init__(self, allowed_root: str):
        self.allowed_root = Path(allowed_root).resolve()
    
    async def execute_tool_call(self, function_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a function call with security validation"""
        if function_name == "read_file":
            return await self._read_file(arguments["file_path"])
        elif function_name == "write_file":
            return await self._write_file(arguments["file_path"], arguments["content"])
        # Add more tool implementations
```

### Phase 4: Prompt Engineering

#### 4.1 System Prompts

Create specialized system prompts for different task types:

```python
def _get_system_prompt(self, task_type: TaskType) -> str:
    """Get system prompt based on task type"""
    base_prompt = """You are an expert software engineer assistant. You have access to file system tools to read, write, and analyze code files."""
    
    if task_type == TaskType.FIX:
        return base_prompt + """
        Your task is to identify and fix bugs in the provided code. 
        - Analyze the code carefully
        - Identify the root cause of issues
        - Implement minimal, focused fixes
        - Preserve existing functionality
        - Test your changes if possible
        """
    elif task_type == TaskType.CODE_REVIEW:
        return base_prompt + """
        Your task is to review code for quality, security, and best practices.
        - Identify potential issues or improvements
        - Check for security vulnerabilities
        - Suggest performance optimizations
        - Verify adherence to coding standards
        """
    # Add more task-specific prompts
```

#### 4.2 Context Management

Implement context window management for large codebases:

```python
def _optimize_context(self, task: Task) -> str:
    """Optimize context to fit within model limits"""
    context_parts = []
    
    # Add essential task information
    context_parts.append(f"Task: {task.title}")
    context_parts.append(f"Type: {task.type.value}")
    
    # Add target files with content truncation if needed
    if task.target_files:
        for file_path in task.target_files[:5]:  # Limit to first 5 files
            try:
                content = self._read_file_content(file_path)
                # Truncate large files
                if len(content) > 2000:
                    content = content[:1000] + "\n\n[... truncated ...]\n\n" + content[-1000:]
                context_parts.append(f"File: {file_path}\n```\n{content}\n```")
            except Exception as e:
                context_parts.append(f"File: {file_path} (error reading: {e})")
    
    return "\n\n".join(context_parts)
```

### Phase 5: Migration Steps

#### 5.1 Dependency Updates

Update `pyproject.toml`:

```toml
dependencies = [
  "watchdog==3.0.0",
  "pydantic==2.5.0", 
  "python-dotenv==1.0.0",
  "pyyaml>=6.0.1,<7",
  "openai>=1.0.0",  # Add OpenAI SDK
  "tiktoken>=0.5.0",  # For token counting
]
```

#### 5.2 Code Changes

1. **Update imports** in `src/orchestrator.py`:
   ```python
   # Replace
   from src.bridges import ClaudeBridge
   # With
   from src.bridges import CodexBridge
   ```

2. **Initialize CodexBridge** instead of ClaudeBridge:
   ```python
   def __init__(self):
       # Replace
       self.claude_bridge = ClaudeBridge()
       # With  
       self.codex_bridge = CodexBridge()
   ```

3. **Update method calls**:
   ```python
   # Replace calls to self.claude_bridge with self.codex_bridge
   result = await self.codex_bridge.execute_task(task)
   ```

#### 5.3 Configuration Migration

Update `config/settings.py` initialization:

```python
def __init__(self):
    # Replace Claude configuration
    self.codex = CodexConfig(
        api_key=os.getenv("OPENAI_API_KEY", ""),
        model=os.getenv("CODEX_MODEL", "gpt-4-turbo"),
        max_tokens=int(os.getenv("CODEX_MAX_TOKENS", "4096")),
        temperature=float(os.getenv("CODEX_TEMPERATURE", "0.1")),
        base_cwd=os.getenv("CODEX_BASE_CWD"),
        allowed_root=os.getenv("CODEX_ALLOWED_ROOT")
    )
```

### Phase 6: Testing and Validation

#### 6.1 Unit Tests

Create test cases for CodexBridge:

```python
# tests/test_codex_bridge.py
import pytest
from src.bridges.codex_bridge import CodexBridge
from src.core import Task, TaskType, TaskPriority

class TestCodexBridge:
    @pytest.fixture
    def codex_bridge(self):
        return CodexBridge()
    
    async def test_execute_simple_task(self, codex_bridge):
        task = Task(
            id="test_001",
            type=TaskType.ANALYZE,
            priority=TaskPriority.MEDIUM,
            title="Test Task",
            prompt="Analyze this simple Python function",
            target_files=["test.py"]
        )
        
        result = await codex_bridge.execute_task(task)
        assert result.task_id == task.id
        assert isinstance(result.success, bool)
```

#### 6.2 Integration Tests

Test end-to-end functionality:

```python
# tests/test_codex_integration.py
async def test_full_pipeline_with_codex():
    orchestrator = TaskOrchestrator()
    
    # Create a test task
    task_id = orchestrator.create_task_from_description(
        "Analyze the main.py file for potential improvements"
    )
    
    # Wait for processing
    await asyncio.sleep(5)
    
    # Verify results
    assert task_id in orchestrator.task_results
    result = orchestrator.task_results[task_id]
    assert result.success
```

### Phase 7: Performance and Security Considerations

#### 7.1 Rate Limiting

Implement token-aware rate limiting:

```python
class TokenBucket:
    """Token bucket for API rate limiting"""
    
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill = time.time()
    
    async def consume(self, tokens: int) -> bool:
        """Consume tokens, return False if not available"""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False
```

#### 7.2 Security Controls

Preserve existing security model:

```python
def _validate_file_access(self, file_path: str) -> bool:
    """Validate file access against allowed root"""
    try:
        resolved_path = Path(file_path).resolve()
        allowed_root = Path(self.config.allowed_root).resolve()
        return allowed_root in resolved_path.parents or resolved_path == allowed_root
    except Exception:
        return False
```

#### 7.3 Cost Monitoring

Add usage tracking:

```python
@dataclass
class UsageMetrics:
    """Track API usage for cost monitoring"""
    total_tokens: int = 0
    total_requests: int = 0
    total_cost: float = 0.0
    
    def add_request(self, input_tokens: int, output_tokens: int, model: str):
        self.total_tokens += input_tokens + output_tokens
        self.total_requests += 1
        self.total_cost += self._calculate_cost(input_tokens, output_tokens, model)
```

## Migration Timeline

### Week 1: Foundation
- [ ] Create CodexBridge class structure
- [ ] Update configuration system
- [ ] Add OpenAI SDK dependency

### Week 2: Core Implementation  
- [ ] Implement API integration
- [ ] Create tool execution framework
- [ ] Add error handling and retries

### Week 3: Integration
- [ ] Update orchestrator integration
- [ ] Implement context management
- [ ] Add rate limiting and security

### Week 4: Testing & Deployment
- [ ] Create comprehensive test suite
- [ ] Performance testing and optimization
- [ ] Documentation and deployment

## Risk Mitigation

### API Availability
- Implement graceful fallback to LLAMA-only mode
- Add circuit breaker pattern for API failures
- Cache successful responses where appropriate

### Cost Management
- Set hard limits on token usage
- Implement request batching where possible
- Monitor costs in real-time

### Security
- Preserve all existing file system security controls
- Add API key rotation capabilities
- Audit all file operations

## Success Metrics

- [ ] All existing task types execute successfully
- [ ] Response quality meets or exceeds Claude performance
- [ ] API costs remain within budget constraints
- [ ] Integration tests pass with >95% success rate
- [ ] Security audit shows no new vulnerabilities

## Rollback Plan

If issues arise during migration:

1. **Immediate Rollback**: Revert bridge imports to use ClaudeBridge
2. **Configuration Rollback**: Restore original ClaudeConfig settings  
3. **Dependency Rollback**: Remove OpenAI dependencies if needed
4. **Data Recovery**: Ensure all task results are preserved during rollback

## Conclusion

This technical approach provides a structured path for replacing Claude with Codex while preserving the AI-Team project's core functionality, security model, and operational characteristics. The phased implementation allows for incremental migration with validation at each step.

The design maintains the existing abstractions through the IClaudeBridge interface, ensuring minimal disruption to the broader system architecture while leveraging Codex's capabilities for code analysis and generation tasks.