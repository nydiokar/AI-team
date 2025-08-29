#!/usr/bin/env python3
"""
Test script for GitFileDetector
"""
import sys
import os
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.core.git_file_detector import GitFileDetector

def main():
    """Test the git file detector"""
    print("Testing Git File Detector...")
    
    try:
        # Initialize detector
        detector = GitFileDetector()
        
        # Detect changes
        changes = detector.detect_file_changes()
        
        print("\n📊 Detected Changes:")
        print(f"  Created: {len(changes['created'])} files")
        print(f"  Modified: {len(changes['modified'])} files")
        print(f"  Deleted: {len(changes['deleted'])} files")
        
        if changes['created']:
            print("\n  Created files:")
            for file_path in changes['created'][:5]:  # Show first 5
                print(f"    + {file_path}")
            if len(changes['created']) > 5:
                print(f"    ... and {len(changes['created']) - 5} more")
        
        if changes['modified']:
            print("\n  Modified files:")
            for file_path in changes['modified'][:5]:  # Show first 5
                print(f"    * {file_path}")
            if len(changes['modified']) > 5:
                print(f"    ... and {len(changes['modified']) - 5} more")
        
        if changes['deleted']:
            print("\n  Deleted files:")
            for file_path in changes['deleted'][:5]:  # Show first 5
                print(f"    - {file_path}")
            if len(changes['deleted']) > 5:
                print(f"    ... and {len(changes['deleted']) - 5} more")
        
        # Get summary
        summary = detector.get_changes_summary(changes)
        print(f"\n📝 Summary:\n{summary}")
        
        print("\n✅ Git detector test completed successfully!")
        return 0
        
    except Exception as e:
        print(f"❌ Error testing git detector: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
