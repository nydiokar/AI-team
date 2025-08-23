#!/usr/bin/env python3
"""
Debug script to see what's happening in the validation engine
"""
from src.validation.engine import ValidationEngine
from src.core.interfaces import TaskType

def debug_validation():
    print("🔍 Debugging Validation Engine")
    print("=" * 40)
    
    engine = ValidationEngine()
    engine.config.similarity_threshold = 0.1
    
    input_text = "Summarize the authentication code"
    output_text = "We applied patch and modified:app/main.py to fix authentication"
    
    print(f"Input: '{input_text}'")
    print(f"Output: '{output_text}'")
    print(f"Task Type: {TaskType.SUMMARIZE}")
    print(f"Similarity threshold: {engine.config.similarity_threshold}")
    
    # Check what edit markers the engine is looking for
    edit_markers = ["apply patch", "edited:", "modified:"]
    print(f"\nEdit markers being checked: {edit_markers}")
    
    # Check if our text contains any of these markers
    output_lower = output_text.lower()
    for marker in edit_markers:
        if marker in output_lower:
            print(f"✅ Found marker: '{marker}' in output")
        else:
            print(f"❌ Marker '{marker}' NOT found in output")
    
    # Run the actual validation
    print(f"\n🔍 Running validation...")
    res = engine.validate_llama_output(input_text, output_text, TaskType.SUMMARIZE)
    
    print(f"Result: {res}")
    print(f"Valid: {res.valid}")
    print(f"Similarity: {res.similarity}")
    print(f"Entropy: {res.entropy}")
    print(f"Issues: {res.issues}")
    
    # Check if the task type condition is met
    print(f"\n🔍 Task type check:")
    print(f"Task type: {TaskType.SUMMARIZE}")
    print(f"Task type in (SUMMARIZE, CODE_REVIEW): {TaskType.SUMMARIZE in (TaskType.SUMMARIZE, TaskType.CODE_REVIEW)}")
    
    # Check the exact string matching
    print(f"\n🔍 String matching:")
    print(f"Output lower: '{output_lower}'")
    print(f"Contains 'modified:': {'modified:' in output_lower}")
    print(f"Contains 'apply patch': {'apply patch' in output_lower}")

if __name__ == "__main__":
    debug_validation()
