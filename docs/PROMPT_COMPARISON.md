# 🔄 Prompt Comparison: Automatic vs Manual Agent Selection

This shows how the same user intent creates different prompts depending on the mode used.

## User Intent
"Create documentation for agent system"

---

## 🤖 Automatic Mode Prompt (via `/task`)

**When using:** `/task Create documentation for agent system`

```
Task: ANALYZE - Task: Create documentation for agent system...

Priority: MEDIUM

Description:
Create documentation for agent system

Target Files: To be discovered (search and identify relevant files first)

Agent Instructions:
Please analyze and provide insights on the requested task. Focus on understanding the current state and providing actionable recommendations.

General Instructions:
1. Follow the agent's specific instructions above
2. If no target files are specified, first identify relevant files by searching the repository
3. Provide clear, actionable output
4. Include a changelog section at the end if files are modified:
   Created: path/to/file
   Modified: path/to/file
5. Note any limitations or issues encountered

Focus on quality, maintainability, and following established code conventions.
```

---

## 📚 Manual Documentation Agent Prompt (via `/documentation`)

**When using:** `/documentation Create documentation for agent system`

```
Our task today consists of Create documentation for agent system for documentation.

Following these core principles:
## When write code and modify files

### All languages

- DO NOT create a class if it is not necessary. Focus on FP. OOP is still ok, but in rare cases.
- Make minimal changes to files - modify only what's necessary to complete the task:
  - Focus on the specific task at hand, avoid unrelated improvements
  - Preserve existing code structure and formatting
  - Make changes in small, verifiable steps
  - Choose solutions that require minimal code changes
  - If needed, write in chat why minimal changes were not possible if larger changes are required and ask for approval
- Follow core software development principles:
  - TDD (Test-Driven Development): Write tests first (or verify it's technically right), then implement the functionality
  - DRY (Don't Repeat Yourself): Avoid code duplication, extract reusable components
  - KISS (Keep It Simple, Stupid): Choose simple solutions over complex ones
  - YAGNI (You Aren't Gonna Need It): Don't implement functionality until it's necessary
  - Big-O Complexity Awareness: Choose optimal computational approaches.
  - Lazy Evaluation: Defer computation until needed.

### When writing in Python:

- Use Python with strict types for each variable 
- Write down types of function results like `-> ...`
- Use Pydantic models for data structures instead of TypedDict or other solutions
- Try to avoid type `Any`
- Do not use `@staticmethod`
- Always prefer functional programming over OOP when possible.
- Use `pyproject.toml` instead of `requirements.txt`.
- When you mention a variable for the first time, try to write down the type. For example, `scopes: list[str] = [...]` (`: list[str]` added).

For this specific documentation task, here are the specialized instructions:

**Documentation Standards:**
- Structure documentation to mirror the codebase architecture
- Break complex concepts into digestible, focused sections  
- Cover all public APIs, configuration options, and usage patterns
- Include practical, working examples for each major feature
- Ensure documentation can be easily updated as code evolves

**Process Approach:**
1. First examine the existing codebase structure and current documentation
2. Identify missing, outdated, or incomplete documentation
3. Plan the documentation structure to serve both new developers and maintainers
4. Create comprehensive documentation with practical examples
5. Validate that examples work and accurately reflect the code
6. Create clear navigation paths and link related documentation

**Quality Checks:**
- Verify all code examples are functional and up-to-date
- Ensure documentation addresses common use cases and edge cases
- Check that technical terminology is explained for newcomers
- Identify and document any inconsistencies or potential improvements in the codebase

Task Details:
- Title: Task: Create documentation for agent system...
- Type: Documentation
- Priority: Medium
- Target Files: To be discovered (search and identify relevant files first)

Let's begin: Create documentation for agent system

Please provide a comprehensive approach that follows both the general principles above and the specific documentation guidelines. Focus on quality, maintainability, and clear communication of what you accomplish.
```

---

## 🔍 Key Differences

### Automatic Mode:
- ✅ **Generic** approach suitable for exploration
- ✅ **Flexible** - may interpret intent differently  
- ✅ **LLAMA-optimized** when available
- ❌ **Less specialized** instructions
- ❌ **May not be documentation-focused**

### Manual Documentation Agent:
- ✅ **Documentation-specific** standards and process
- ✅ **Predictable** - always follows documentation approach
- ✅ **Comprehensive** guidelines for quality docs
- ✅ **Includes your coding principles** for any code examples
- ❌ **More verbose** (but more comprehensive)

## 🎯 When to Choose Each

**Use Manual Documentation Agent** (`/documentation`) when:
- You specifically need documentation created or updated
- You want consistent documentation standards applied
- You need comprehensive coverage with examples
- You want the focus to stay on documentation quality

**Use Automatic Mode** (`/task`) when:
- You're unsure if documentation is the best approach
- Your request might need analysis first
- You want the system to determine the optimal approach
- Your task combines documentation with other work

Both modes will accomplish the goal, but the manual agent provides more specialized, focused guidance!