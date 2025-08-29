#!/usr/bin/env python3
"""
Test script for the Claude Session Parser
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

def test_session_parser():
    """Test the Claude session parser functionality"""
    print("Testing Claude Session Parser...")
    
    try:
        from src.bridges.claude_session_parser import ClaudeSessionParser
        
        # Initialize parser
        parser = ClaudeSessionParser()
        
        if not parser.claude_projects_dir:
            print("❌ Claude projects directory not found")
            print("   This is expected if Claude is not installed or configured")
            return False
        
        print(f"✅ Found Claude projects directory: {parser.claude_projects_dir}")
        
        # Test finding session files for current directory
        current_dir = str(Path.cwd())
        print(f"\nLooking for session files relevant to: {current_dir}")
        
        session_files = parser.find_session_files(current_dir)
        print(f"Found {len(session_files)} relevant session files")
        
        if session_files:
            print("Session files found:")
            for i, session_file in enumerate(session_files[:5]):  # Show first 5
                print(f"  {i+1}. {session_file.name}")
                if i == 4 and len(session_files) > 5:
                    print(f"  ... and {len(session_files) - 5} more")
            
            # Test parsing file changes
            print("\nParsing file changes from session files...")
            changes = parser.parse_file_changes(session_files)
            
            print(f"File changes detected:")
            print(f"  Created: {len(changes['created'])} files")
            print(f"  Modified: {len(changes['modified'])} files")
            print(f"  Deleted: {len(changes['deleted'])} files")
            print(f"  Tool uses: {len(changes['tool_uses'])} operations")
            
            # Show summary
            summary = parser.get_changes_summary(changes)
            print(f"\nSummary:\n{summary}")
            
        else:
            print("No session files found - this is normal if no Claude sessions exist yet")
        
        print("\n✅ Claude Session Parser test completed successfully!")
        return True
        
    except Exception as e:
        print(f"❌ Claude Session Parser test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run the test"""
    print("🧪 Testing Claude Session Parser\n")
    
    if test_session_parser():
        print("\n🎉 Session parser is working correctly!")
        return 0
    else:
        print("\n💥 Session parser test failed. Check the errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
