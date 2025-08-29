#!/usr/bin/env python3
"""
Test script for the new modular agent system
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

def test_agent_manager():
    """Test the agent manager functionality"""
    print("Testing Agent Manager...")
    
    try:
        from src.core.agent_manager import AgentManager
        
        # Initialize agent manager
        agent_manager = AgentManager()
        
        # Test getting all agents
        agents = agent_manager.get_all_agents()
        print(f"Loaded {len(agents)} agents:")
        for name, agent in agents.items():
            print(f"  - {name}: {agent.get_agent_name()}")
            print(f"    Tools: {agent.get_allowed_tools()}")
            print(f"    Modifies files: {agent.should_modify_files()}")
            print(f"    Validation thresholds: {agent.get_validation_thresholds()}")
            print()
        
        # Test task type mapping
        from src.core.interfaces import TaskType
        
        test_types = [TaskType.ANALYZE, TaskType.FIX, TaskType.CODE_REVIEW, TaskType.DOCUMENTATION]
        for task_type in test_types:
            agent = agent_manager.get_agent_for_task_type(task_type)
            if agent:
                print(f"Task type {task_type.value} -> Agent: {agent.get_agent_name()}")
            else:
                print(f"Task type {task_type.value} -> No agent found")
        
        print("\n✅ Agent Manager test passed!")
        return True
        
    except Exception as e:
        print(f"❌ Agent Manager test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_llama_mediator():
    """Test the LLAMA mediator with the new agent system"""
    print("\nTesting LLAMA Mediator...")
    
    try:
        from src.bridges.llama_mediator import LlamaMediator
        
        # Initialize mediator
        mediator = LlamaMediator()
        
        # Test prompt creation with different task types
        test_tasks = [
            {
                'type': 'analyze',
                'title': 'Test Analysis',
                'priority': 'medium',
                'main_request': 'Analyze the codebase structure',
                'target_files': []
            },
            {
                'type': 'fix',
                'title': 'Test Bug Fix',
                'priority': 'high',
                'main_request': 'Fix the authentication bug',
                'target_files': ['src/auth.py']
            },
            {
                'type': 'documentation',
                'title': 'Test Documentation',
                'priority': 'low',
                'main_request': 'Create API documentation',
                'target_files': []
            }
        ]
        
        for i, task in enumerate(test_tasks):
            print(f"\nTest {i+1}: {task['type']} task")
            prompt = mediator._create_prompt_with_template(task)
            print(f"Prompt length: {len(prompt)} characters")
            print(f"Contains agent instructions: {'Agent Instructions:' in prompt}")
            print(f"Contains general instructions: {'General Instructions:' in prompt}")
        
        print("\n✅ LLAMA Mediator test passed!")
        return True
        
    except Exception as e:
        print(f"❌ LLAMA Mediator test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all tests"""
    print("🧪 Testing Modular Agent System\n")
    
    tests = [
        test_agent_manager,
        test_llama_mediator
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
    
    print(f"\n📊 Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! The modular agent system is working correctly.")
        return 0
    else:
        print("💥 Some tests failed. Please check the errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
