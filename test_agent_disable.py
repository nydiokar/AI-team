#!/usr/bin/env python3
"""
Test script to verify agent disable functionality
"""
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

def test_agents_disabled():
    """Test that agents are properly disabled when AGENTS_ENABLED=false"""
    print("Testing Agent Disable Functionality...")
    
    try:
        # Set environment variable to disable agents
        os.environ["AGENTS_ENABLED"] = "false"
        
        # Import and test agent manager
        from src.core.agent_manager import AgentManager
        
        # Initialize agent manager
        agent_manager = AgentManager()
        
        # Check that no agents were loaded
        agents = agent_manager.get_all_agents()
        print(f"Agents loaded when disabled: {len(agents)}")
        
        if len(agents) == 0:
            print("✅ Agents properly disabled - no agents loaded")
        else:
            print("❌ Agents not properly disabled - agents still loaded")
            return False
        
        # Test that get_agent returns None
        agent = agent_manager.get_agent("analyze")
        if agent is None:
            print("✅ get_agent returns None when agents disabled")
        else:
            print("❌ get_agent should return None when agents disabled")
            return False
        
        # Test that get_agent_for_task_type returns None
        from src.core.interfaces import TaskType
        agent = agent_manager.get_agent_for_task_type(TaskType.ANALYZE)
        if agent is None:
            print("✅ get_agent_for_task_type returns None when agents disabled")
        else:
            print("❌ get_agent_for_task_type should return None when agents disabled")
            return False
        
        print("\n✅ Agent disable test passed!")
        return True
        
    except Exception as e:
        print(f"❌ Agent disable test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Clean up environment variable
        if "AGENTS_ENABLED" in os.environ:
            del os.environ["AGENTS_ENABLED"]

def test_agents_enabled():
    """Test that agents are properly enabled when AGENTS_ENABLED=true (default)"""
    print("\nTesting Agent Enable Functionality...")
    
    try:
        # Ensure environment variable is not set (default behavior)
        if "AGENTS_ENABLED" in os.environ:
            del os.environ["AGENTS_ENABLED"]
        
        # Import and test agent manager
        from src.core.agent_manager import AgentManager
        
        # Initialize agent manager
        agent_manager = AgentManager()
        
        # Check that agents were loaded
        agents = agent_manager.get_all_agents()
        print(f"Agents loaded when enabled: {len(agents)}")
        
        if len(agents) > 0:
            print("✅ Agents properly enabled - agents loaded")
        else:
            print("❌ Agents not properly enabled - no agents loaded")
            return False
        
        # Test that get_agent returns an agent
        agent = agent_manager.get_agent("analyze")
        if agent is not None:
            print("✅ get_agent returns agent when agents enabled")
        else:
            print("❌ get_agent should return agent when agents enabled")
            return False
        
        print("\n✅ Agent enable test passed!")
        return True
        
    except Exception as e:
        print(f"❌ Agent enable test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all tests"""
    print("🧪 Testing Agent Disable/Enable Functionality\n")
    
    # Test agents disabled
    disabled_ok = test_agents_disabled()
    
    # Test agents enabled
    enabled_ok = test_agents_enabled()
    
    if disabled_ok and enabled_ok:
        print("\n🎉 All tests passed! The agent disable functionality is working correctly.")
        return 0
    else:
        print("\n❌ Some tests failed. Please check the implementation.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
