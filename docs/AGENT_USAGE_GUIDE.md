# 🤖 AI Agent Usage Guide

## Overview

Your AI Task Orchestrator now supports **hybrid agent selection** - you can either let the system automatically choose the best approach, or manually select specific agents via Telegram commands.

## 🔄 Two Operating Modes

### 1. **Automatic Mode** (existing functionality)
- Use `/task <description>` or send natural language messages
- System analyzes intent and selects appropriate approach
- Uses LLAMA for intelligent task expansion and optimization

### 2. **Manual Mode** (new functionality) 
- Use specific agent commands: `/documentation`, `/code_review`, `/bug_fix`, `/analyze`
- Bypasses automatic classification 
- Directly applies your chosen agent's specialized instructions

## 📱 Telegram Commands

### Manual Agent Selection

| Command | Purpose | Example |
|---------|---------|---------|
| `/documentation <intent>` | Create/update documentation | `/documentation Create API docs for auth module` |
| `/code_review <intent>` | Perform code review | `/code_review Review security in auth handlers` |
| `/bug_fix <intent>` | Fix bugs/issues | `/bug_fix Fix memory leak in data processor` |
| `/analyze <intent>` | Analyze and provide insights | `/analyze Performance analysis of database queries` |

### General Commands

| Command | Purpose | Example |
|---------|---------|---------|
| `/task <description>` | Automatic mode | `/task Review the authentication code` |
| `/status` | System status | `/status` |
| `/progress <task_id>` | Task progress | `/progress task_abc123` |
| `/cancel <task_id>` | Cancel task | `/cancel task_abc123` |

### Git Integration Commands

| Command | Purpose | Example |
|---------|---------|---------|
| `/commit <task_id>` | Commit task changes | `/commit task_abc123 --push` |
| `/commit-all <task_id>` | Commit all staged changes | `/commit-all task_abc123` |
| `/git-status` | Show git status | `/git-status` |

## 🎯 Agent Specializations

### Documentation Agent
**Best for:** Creating docs, README files, API documentation, user guides

**Specialized approach:**
- Mirrors codebase architecture in documentation structure
- Includes practical, working examples
- Covers common use cases and edge cases
- Ensures documentation can evolve with code

**Example:**
```
/documentation Create comprehensive setup guide for new developers
```

### Code Review Agent  
**Best for:** Security audits, code quality checks, best practice validation

**Specialized approach:**
- Security-focused (input validation, authorization, secrets)
- Performance and resource usage analysis
- Error handling and logging completeness
- Test coverage and maintainability assessment

**Example:**
```
/code_review Security audit of payment processing module
```

### Bug Fix Agent
**Best for:** Fixing specific issues, debugging problems, error resolution

**Specialized approach:**
- Reproduces issues reliably before fixing
- Implements minimal, targeted changes
- Writes/updates tests to prevent regression
- Documents fixes for future reference

**Example:**
```
/bug_fix Crash when processing empty user input in parser
```

### Analysis Agent
**Best for:** Understanding systems, identifying improvements, technical research

**Specialized approach:**
- Comprehensive current state assessment
- Identifies patterns and anti-patterns
- Proposes concrete, incremental improvements
- Includes risk assessment and implementation roadmap

**Example:**
```
/analyze Database performance bottlenecks in user management
```

## 🔄 How The Hybrid System Works

### Manual Agent Flow:
```
/documentation "Create API docs" 
    ↓
Telegram Interface
    ↓  
Orchestrator (adds agent_type metadata)
    ↓
LlamaMediator detects agent_type
    ↓
Bypasses automatic processing  
    ↓
Uses agent-specific instructions + general principles
    ↓
Creates natural prompt for Claude
    ↓
Claude executes with specialized guidance
```

### Automatic Mode Flow:
```
/task "Review auth code"
    ↓
Telegram Interface
    ↓
Orchestrator (no agent_type)
    ↓
LlamaMediator uses automatic processing
    ↓
LLAMA analyzes intent and selects approach
    ↓
Creates optimized prompt
    ↓
Claude executes
```

## 💡 When to Use Each Mode

### Use Manual Mode When:
- ✅ You know exactly what type of work needs to be done
- ✅ You want specialized, focused instructions
- ✅ You need consistent approach for similar tasks
- ✅ You want to bypass automatic classification

### Use Automatic Mode When:
- ✅ You're exploring or unsure of the best approach
- ✅ Your task spans multiple types of work
- ✅ You want the system to optimize the prompt
- ✅ You prefer natural language interaction

## 🛠️ Working with Files and Directories

### Specifying Working Directory:
```
/documentation Create docs for the payment system in C:\Users\projects\ecommerce
```

### Attaching Files:
- Attach documents to Telegram messages when using agent commands
- Files are automatically copied to the working directory
- Referenced in the task for Claude to access

### File Discovery:
- If no target files specified, agents will search and identify relevant files
- Use specific file paths when you know exactly what needs attention

## 🎯 Best Practices

### For Documentation Tasks:
```
/documentation Create setup guide covering installation, configuration, and first-time usage
```
- Be specific about what type of documentation you need
- Mention the target audience (developers, users, administrators)

### For Code Reviews:
```
/code_review Focus on security and performance in the API authentication layer
```
- Specify focus areas (security, performance, maintainability)
- Identify the scope (specific modules, features, or files)

### For Bug Fixes:
```
/bug_fix Memory leak in background worker process - investigate and fix
```
- Describe the symptoms and when they occur
- Include any error messages or logs you have

### For Analysis:
```
/analyze Database query patterns and recommend optimization strategies
```
- State what you want to understand or improve
- Mention if you need specific recommendations or just assessment

## 📊 Monitoring and Results

### Track Progress:
```
/progress task_abc123
```

### View Results:
- Results saved to `results/{task_id}.json`
- Summaries in `summaries/{task_id}_summary.txt` 
- Telegram notifications on completion

### Commit Changes:
```
/commit task_abc123 --push
```
- Safely commits only task-related changes
- Creates feature branches automatically
- Blocks sensitive files from commits

## 🔧 Advanced Usage

### Environment Variables:
```bash
# Enable/disable agent system
AGENTS_ENABLED=true

# Telegram configuration
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_ALLOWED_USERS=123456789,987654321
```

### Configuration:
- General coding principles in `prompts/general_prompt_coding.md`
- Agent-specific templates in `prompts/agents/`
- Working directory and security settings in `.env`

## 🚀 Getting Started

1. **Set up Telegram bot** with your token and allowed users
2. **Start the orchestrator**: `python main.py`
3. **Try manual mode**: `/documentation Create a quick start guide`
4. **Try automatic mode**: `/task Help me understand this authentication system`
5. **Monitor progress**: `/progress task_id`
6. **Commit results**: `/commit task_id --push`

The system preserves all your existing functionality while adding powerful manual control when you need it!