# Test Files for Dual Prompt System

## Essential Tests

### `full_prompt_test.py` 
**Purpose**: Show complete prompt content without truncation to understand what LLAMA vs Template generates
**Usage**: `python full_prompt_test.py`
**What it reveals**: 
- Full prompt content (no truncation)
- Structure analysis (does it follow "Our task today..." format?)
- LLAMA vs Template mode routing

### `tests/test_unified_prompts.py`
**Purpose**: Original test for automatic vs manual agent modes
**Usage**: `python tests/test_unified_prompts.py` 
**What it tests**: Comparison between automatic and manual agent selection

## Key Questions to Answer

1. **Is LLAMA generating the right structure?**
   - Should start with "Our task today consists of..."
   - Should have "Following these core principles:"
   - Should have "Task Details:" section

2. **Is the 353 char vs 4072 char difference significant?**
   - Short = LLAMA generated (might be incomplete)
   - Long = Template generated (full structure)

3. **Is LLaMA 3.2 2B too small for this task?**
   - Small models struggle with complex prompt generation
   - May need stronger model (7B+ recommended)

## Debugging Notes

- Current routing: LLAMA mode when `ollama_available=True`
- Template mode when LLAMA fails or unavailable
- Agent files are loading correctly (227-500 chars each)
- Issue likely in `_build_prompt_with_llama()` meta-prompt or model capability

## Next Steps

1. Run `full_prompt_test.py` on workstation with full LLAMA model
2. Compare output structure between LLAMA and Template modes  
3. If structure is wrong, fix meta-prompt in `_build_prompt_with_llama()`
4. If model is too small, recommend upgrading to 7B+ model