     ╔══════════════════════════════════════════════════════════════╗
     ║                    🤖 CLAUDE AGENT ORCHESTRATOR              ║
     ║                         "The Coding Engine"                  ║
     ╚══════════════════════════════════════════════════════════════╝
                                      │
                           ┌─────────────────┐
                           │ USER INPUT      │◄─── "Please document this project"
                           │ RECEPTACLE      │
                           └─────────┬───────┘
                                     │
                         ╭───────────▼────────────╮
                        ╱   INTENT ANALYZER GEAR   ╲
                       ╱    "Our task today       ╱
                      ╱   consists of [INTENT]"  ╱
                     ╱_________________________╱
                                     │
                      ┌──────────────▼─────────────┐
                      │      TASK CLASSIFIER       │
                      │   ┌───────┬───────┬───────┐ │
                      │   │  DOC  │ DEBUG │ CODE  │ │
                      │   └───────┴───────┴───────┘ │
                      └──────┬─────┬─────┬─────────┘
                             │     │     │
            ╔════════════════▼═══╗ │     │
            ║  📚 DOCUMENTATION  ║ │     │
            ║      ASSEMBLY      ║ │     │
            ║                   ║ │     │
            ║ ┌───────────────┐ ║ │     │
            ║ │ General       │ ║ │     │
            ║ │ Principles    │◄─╫─┼─────┼── prompts/general_prompt_coding.md
            ║ │ Injector      │ ║ │     │
            ║ └───────────────┘ ║ │     │
            ║         │         ║ │     │
            ║ ┌───────▼───────┐ ║ │     │
            ║ │ Task-Specific │ ║ │     │
            ║ │ Instructions  │ ║ │     │
            ║ │ Generator     │ ║ │     │
            ║ │              │ ║ │     │
            ║ │"...and by    │ ║ │     │
            ║ │following     │ ║ │     │
            ║ │these         │ ║ │     │
            ║ │principles"   │ ║ │     │
            ║ └───────────────┘ ║ │     │
            ╚═════════┬═════════╝ │     │
                      │           │     │
                  ╔═══▼═══════════▼═══╗ │
                  ║  🐛 DEBUG ENGINE  ║ │
                  ║                  ║ │
                  ║ ┌──────────────┐ ║ │
                  ║ │ Bug Pattern  │ ║ │
                  ║ │ Recognition  │ ║ │
                  ║ └──────────────┘ ║ │
                  ║        │         ║ │
                  ║ ┌──────▼──────┐  ║ │
                  ║ │ Fix Strategy │  ║ │
                  ║ │ Synthesizer  │  ║ │
                  ║ └─────────────┘  ║ │
                  ╚═══════════┬══════╝ │
                              │        │
                          ╔═══▼════════▼═══╗
                          ║ 💻 CODE ENGINE ║
                          ║               ║
                          ║ ┌───────────┐ ║
                          ║ │ Function  │ ║
                          ║ │ Factory   │ ║
                          ║ └───────────┘ ║
                          ║      │        ║
                          ║ ┌────▼────┐   ║
                          ║ │ Type    │   ║
                          ║ │ Forger  │   ║
                          ║ └─────────┘   ║
                          ╚═══════┬═══════╝
                                  │
                   ╔══════════════▼═══════════════╗
                   ║    🔧 PROMPT SYNTHESIZER     ║
                   ║                             ║
                   ║  "Our task today consists   ║
                   ║   of [EXPANDED_INTENT] for  ║
                   ║   [TASK_TYPE]. Following    ║
                   ║   these principles:         ║
                   ║   [GENERAL_PRINCIPLES] and  ║
                   ║   these specific guidelines:║
                   ║   [TASK_INSTRUCTIONS]"      ║
                   ╚═════════════┬═══════════════╝
                                 │
                      ┌──────────▼──────────┐
                      │   🚀 CLAUDE BRIDGE  │
                      │                     │
                      │ ┌─────────────────┐ │
                      │ │ Message Formatter│ │
                      │ └─────────────────┘ │
                      │          │          │
                      │ ┌────────▼────────┐ │
                      │ │ Response Parser │ │
                      │ └─────────────────┘ │
                      └─────────┬───────────┘
                                │
                      ┌─────────▼─────────┐
                      │  📊 RESULT PARSER │
                      │                   │
                      │ files_modified: ✓ │
                      │ summary: ✓        │
                      │ success: ✓        │
                      └─────────┬─────────┘
                                │
                      ┌─────────▼─────────┐
                      │ 📤 OUTPUT RELAY   │
                      │                   │
                      │ ┌───────────────┐ │
                      │ │ Telegram Bot  │ │
                      │ │ Notifier      │ │
                      │ └───────────────┘ │
                      └───────────────────┘

● 🔧 COMPONENT BREAKDOWN:

  Intent Analyzer Gear: Takes raw user input and extracts the core intent
  - Input: "Please document this project"
  - Output: [INTENT] = "create comprehensive documentation"

  Task Classifier: Routes to appropriate specialized engine
  - DOC = Documentation tasks
  - DEBUG = Bug fixing/code review
  - CODE = New feature/refactoring

  General Principles Injector: Always injects your coding standards
  - Source: prompts/general_prompt_coding.md
  - Ensures consistent quality across all agents

  Task-Specific Instructions Generator: Adds specialized guidance
  - DOC: "respect code structure, break into granular pieces, reflect inconsistencies"
  - DEBUG: "identify root cause, suggest minimal fixes, verify solution"
  - CODE: "follow TDD, use strict types, prefer FP over OOP"

● Prompt Synthesizer: The heart of the machine - combines all layers:
  "Our task today consists of [EXPANDED_INTENT] for [TASK_TYPE].
  Following these principles: [GENERAL_PRINCIPLES] and
  these specific guidelines: [TASK_INSTRUCTIONS]"

  Claude Bridge: Handles the actual Claude API communication
  - Formats the synthesized prompt
  - Manages conversation flow
  - Parses responses for structured data

  Result Parser: Extracts key information for your orchestrator
  - files_modified: List of changed files
  - summary: What was accomplished
  - success: Whether task completed

● 📋 BOILERPLATE MAPPING:

  - "Our task today consists of [INTENT]" → Intent Analyzer Gear output
  - "[TASK_TYPE]" → Task Classifier routing decision
  - "Following these principles" → General Principles Injector
  - "[GENERAL_PRINCIPLES]" → Your general_prompt_coding.md content
  - "these specific guidelines" → Task-Specific Instructions Generator
  - "[TASK_INSTRUCTIONS]" → Specialized agent instructions

  This creates a natural conversation flow while maintaining the layered instruction system you wanted!

> and how would finally the prompt look like if we need to make a coherent prompt at the end that is including the
  user prompt. Basically to reverse engineer the flow now and make the actually definitive prompt that we are going to
   create for the llama mediator, that is as it's name is telling to mediate between these several things - intent,
  task type, general princoples, task instructions these are like several pieces and as I understand this our task
  here is tailoring the mediator to jungle efficiently with these components and make sure claude receives followable
  and coherent instructions ?

● Exactly! Let me reverse engineer this and show you the LLama Mediator Prompt that orchestrates everything, then
  the Final Claude Prompt that gets generated.

  🦙 LLAMA MEDIATOR PROMPT TEMPLATE

  You are an AI agent mediator responsible for crafting coherent, actionable prompts for Claude based on user
  requests. Your job is to analyze the user's intent, classify the task type, and synthesize appropriate
  instructions.

  ## Your Components to Work With:

  ### 1. INTENT ANALYSIS
  Extract and expand the user's core intent into specific, actionable objectives.

  ### 2. TASK CLASSIFICATION
  Classify into one of these types:
  - DOCUMENTATION: Creating/updating docs, README files, API documentation
  - DEBUG: Bug fixing, code review, error investigation
  - CODE: New features, refactoring, implementation tasks
  - OPTIMIZATION: Performance improvements, code quality enhancements

  ### 3. GENERAL PRINCIPLES (Always Include)
  {GENERAL_PRINCIPLES_FROM_FILE}

  ### 4. TASK-SPECIFIC INSTRUCTIONS
  Based on task type, add specialized guidance:

  **DOCUMENTATION:**
  - Respect existing code structure and break into granular, digestible sections
  - Identify and document any inconsistencies or potential improvements
  - Create comprehensive yet concise documentation that serves both new developers and maintainers
  - Include practical examples and usage patterns where applicable

  **DEBUG:**
  - Systematically identify root causes before proposing solutions
  - Suggest minimal, targeted fixes that don't introduce new issues
  - Verify solutions against existing test suites and create new tests if needed
  - Document the debugging process and reasoning for future reference

  **CODE:**
  - Follow TDD principles: understand requirements, write/verify tests, then implement
  - Use strict typing and prefer functional programming approaches
  - Make minimal changes to existing codebase while achieving the objective
  - Ensure new code integrates seamlessly with existing patterns and conventions

  **OPTIMIZATION:**
  - Profile and measure before optimizing to identify actual bottlenecks
  - Prioritize algorithmic improvements over micro-optimizations
  - Maintain code readability and maintainability while improving performance
  - Document performance gains and trade-offs made

  ## Your Task:
  Given the user input: "{USER_INPUT}"

  Generate a coherent, natural prompt for Claude that:
  1. Starts with conversational context about today's task
  2. Clearly states the expanded intent and task type
  3. Integrates general principles seamlessly
  4. Includes relevant task-specific instructions
  5. Maintains a natural, collaborative tone

  Format your response as a single, cohesive prompt ready to send to Claude.

● 📝 EXAMPLE: FINAL CLAUDE PROMPT GENERATED

  User Input: "Please document this project and commit the changes"

  LLama Mediator Output:

  ---
  Our task today consists of creating comprehensive documentation for this project, ensuring it's properly
  structured and committed to the repository for team accessibility.

  This is a DOCUMENTATION task that requires us to analyze the existing codebase, understand its architecture, and
  create clear, maintainable documentation that will serve both new developers and current maintainers.

  As we work on this, let's follow these core principles that ensure high-quality results:

  Code Quality Standards:
  - Focus on functional programming approaches where possible, avoiding unnecessary class creation
  - Make minimal, targeted changes - preserve existing code structure and formatting
  - Follow TDD principles: verify our approach is technically sound before implementing
  - Maintain DRY, KISS, and YAGNI principles throughout our work
  - Use strict typing and avoid Any types when working with Python code
  - Always prefer editing existing files over creating new ones unless absolutely necessary

  Documentation Best Practices:
  For this documentation task specifically, we need to:
  - Respect the existing code structure and break our documentation into granular, digestible sections that mirror
  the codebase organization
  - Identify and document any inconsistencies or potential improvements we discover during our analysis
  - Create comprehensive yet concise documentation that includes practical examples and usage patterns where
  applicable
  - Ensure our documentation serves both newcomers who need to understand the system and maintainers who need quick
  reference

  Process Approach:
  Start by reading the README.md and exploring the project structure to understand the current state. Then
  systematically document each major component, API endpoints, configuration options, and usage examples. Finally,
  commit our changes with a clear, descriptive commit message that follows the project's existing commit
  conventions.

  Let's begin by examining the project structure and existing documentation to understand what we're working with.

  ---

● 🔄 HOW COMPONENTS COMBINE COHERENTLY

  The magic is in the seamless flow:

  1. Natural Opening: "Our task today consists of..." feels conversational
  2. Intent Expansion: Takes "document this project" → "creating comprehensive documentation for team accessibility"
  3. Task Classification: Clearly states this is a DOCUMENTATION task
  4. Smooth Transition: "As we work on this, let's follow these core principles..."
  5. General Principles: Your coding standards integrated naturally as "quality standards"
  6. Specific Instructions: Documentation-specific guidance flows logically
  7. Actionable Steps: Ends with concrete next steps Claude can immediately follow

  Key Success Factors:
  - No abrupt transitions between instruction layers
  - Each section builds on the previous one
  - Maintains conversational tone throughout
  - Gives Claude both context AND specific actionable steps
  - Feels like working with an experienced colleague, not following a template

● This LLama mediator approach gives you:
  - ✅ Natural, coherent prompts instead of templated instructions
  - ✅ Consistent quality through your general principles