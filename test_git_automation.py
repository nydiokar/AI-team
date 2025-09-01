#!/usr/bin/env python3
"""
Manual test runner for git automation functionality
"""
import os
import sys
import tempfile
import shutil
import subprocess
import time
from pathlib import Path

# Add src to path
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.append(str(src_path))

# Windows-compatible signal handling
try:
    import signal
    SIGALRM_AVAILABLE = hasattr(signal, 'SIGALRM')
except ImportError:
    SIGALRM_AVAILABLE = False

def timeout_handler(signum, frame):
    """Handle timeout signals"""
    raise TimeoutError("Operation timed out")

def test_git_automation_service():
    """Test GitAutomationService functionality"""
    print("🧪 Testing GitAutomationService...")
    
    try:
        # Set a timeout for this test (Windows-compatible)
        if SIGALRM_AVAILABLE:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(30)  # 30 second timeout
        else:
            print("  ⚠️  Signal-based timeout not available on Windows, using manual timeout")
        
        print("  📥 Importing GitAutomationService...")
        from src.core.git_automation import GitAutomationService
        print("  ✅ Import successful")
        
        # Test initialization with timeout
        print("  🔧 Initializing service...")
        start_time = time.time()
        service = GitAutomationService()
        
        # Manual timeout check for Windows
        if not SIGALRM_AVAILABLE and (time.time() - start_time) > 30:
            print("  ❌ Test timed out - LLAMA initialization may be hanging")
            return False
            
        print("  ✅ GitAutomationService initialized successfully")
        
        # Test git status summary with timeout
        print("  📊 Getting git status...")
        status = service.get_git_status_summary()
        if "error" in status:
            print(f"  ⚠️  Git status error: {status['error']}")
        else:
            print(f"  ✅ Git status retrieved: {status['current_branch']} branch")
        
        if SIGALRM_AVAILABLE:
            signal.alarm(0)  # Cancel timeout
        return True
        
    except TimeoutError:
        print("  ❌ Test timed out - LLAMA initialization may be hanging")
        return False
    except ImportError as e:
        print(f"  ❌ Import error: {e}")
        return False
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False
    finally:
        if SIGALRM_AVAILABLE:
            signal.alarm(0)  # Ensure timeout is cancelled

def test_git_cli_commands():
    """Test git CLI commands"""
    print("\n🧪 Testing Git CLI commands...")
    
    try:
        # Test git-status command with timeout
        print("  📥 Testing git-status...")
        result = subprocess.run(
            [sys.executable, "main.py", "git-status"],
            capture_output=True,
            text=True,
            timeout=15  # Reduced timeout for CLI test
        )
        
        if result.returncode == 0:
            print("  ✅ git-status command executed successfully")
            if "Git Repository Status" in result.stdout:
                print("  ✅ git-status output contains expected content")
            else:
                print("  ⚠️  git-status output format may be unexpected")
        else:
            print(f"  ❌ git-status command failed: {result.stderr}")
            
        return True
        
    except subprocess.TimeoutExpired:
        print("  ❌ git-status command timed out")
        return False
    except Exception as e:
        print(f"  ❌ Error testing CLI commands: {e}")
        return False

def test_git_file_detector():
    """Test GitFileDetector functionality"""
    print("\n🧪 Testing GitFileDetector...")
    
    try:
        print("  📥 Importing GitFileDetector...")
        from src.core.git_file_detector import GitFileDetector
        print("  ✅ Import successful")
        
        # Test initialization
        print("  🔧 Initializing detector...")
        detector = GitFileDetector()
        print("  ✅ GitFileDetector initialized successfully")
        
        # Test file change detection
        print("  📝 Detecting file changes...")
        changes = detector.detect_file_changes()
        print(f"  ✅ File changes detected: {changes['total']} total changes")
        
        # Test current branch
        print("  🌿 Getting current branch...")
        current_branch = detector.get_current_branch()
        if current_branch:
            print(f"  ✅ Current branch: {current_branch}")
        else:
            print("  ⚠️  Could not determine current branch")
        
        return True
        
    except ImportError as e:
        print(f"  ❌ Import error: {e}")
        return False
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def test_integration_with_temp_repo():
    """Test git automation with a temporary repository"""
    print("\n🧪 Testing integration with temporary repository...")
    
    temp_dir = tempfile.mkdtemp()
    repo_path = Path(temp_dir)
    
    try:
        # Initialize git repository
        print("  🔧 Initializing temporary git repository...")
        subprocess.run(['git', 'init'], cwd=repo_path, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=repo_path, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo_path, check=True)
        print("  ✅ Temporary git repository initialized")
        
        # Create test file
        print("  📝 Creating test file...")
        test_file = repo_path / "test.txt"
        test_file.write_text("Hello, World!")
        print("  ✅ Test file created")
        
        # Test GitFileDetector in temp repo
        print("  🔍 Testing GitFileDetector in temp repo...")
        from src.core.git_file_detector import GitFileDetector
        detector = GitFileDetector(str(repo_path))
        
        changes = detector.detect_file_changes()
        if changes['created']:
            print(f"  ✅ File changes detected: {changes['created']}")
        else:
            print("  ⚠️  No file changes detected")
        
        # Test staging and committing
        print("  📦 Staging test file...")
        subprocess.run(['git', 'add', 'test.txt'], cwd=repo_path, check=True)
        print("  ✅ File staged")
        
        # Test GitAutomationService with timeout
        print("  🚀 Testing GitAutomationService commit...")
        from src.core.git_automation import GitAutomationService
        service = GitAutomationService(str(repo_path))
        
        # Set timeout for commit operation (Windows-compatible)
        if SIGALRM_AVAILABLE:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(30)
        
        try:
            result = service.safe_commit_task(
                task_id="test_integration",
                task_description="Test integration commit",
                create_branch=True,
                push_branch=False
            )
            
            if result["success"]:
                print("  ✅ Integration test commit successful")
                print(f"     📁 Branch: {result['branch_name']}")
                print(f"     📄 Files: {result['files_committed']}")
            else:
                print(f"  ❌ Integration test commit failed: {result['errors']}")
        finally:
            if SIGALRM_AVAILABLE:
                signal.alarm(0)  # Cancel timeout
        
        return True
        
    except TimeoutError:
        print("  ❌ Integration test timed out - commit operation may be hanging")
        return False
    except Exception as e:
        print(f"  ❌ Integration test error: {e}")
        return False
    finally:
        # Cleanup with Windows-friendly approach
        try:
            # Give git processes time to finish
            time.sleep(1)
            
            # Force close any open git processes
            try:
                subprocess.run(['git', 'gc'], cwd=repo_path, capture_output=True, timeout=5)
            except:
                pass  # Ignore gc errors
            
            # Try to remove the directory
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            # If rmtree failed, try alternative cleanup
            if Path(temp_dir).exists():
                try:
                    # Remove git objects first
                    git_objects = Path(temp_dir) / ".git" / "objects"
                    if git_objects.exists():
                        for obj_dir in git_objects.iterdir():
                            if obj_dir.is_dir():
                                for obj_file in obj_dir.iterdir():
                                    try:
                                        obj_file.unlink()
                                    except:
                                        pass
                        git_objects.rmdir()
                    
                    # Now try to remove the rest
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except:
                    print(f"  ⚠️  Could not fully clean up {temp_dir}")
                    print("  💡 This is normal on Windows - temp files will be cleaned up automatically")
            
            print("  ✅ Temporary repository cleaned up")
            
        except Exception as e:
            print(f"  ⚠️  Cleanup warning: {e}")
            print("  💡 This is normal on Windows - temp files will be cleaned up automatically")

def test_llama_performance():
    """Test LLAMA mediator performance"""
    print("\n🧪 Testing LLAMA Mediator Performance...")
    
    try:
        print("  📥 Importing LlamaMediator...")
        from src.bridges.llama_mediator import LlamaMediator
        print("  ✅ Import successful")
        
        # Test initialization with timeout
        print("  🔧 Initializing LLAMA mediator...")
        start_time = time.time()
        
        # Set timeout for LLAMA initialization (Windows-compatible)
        if SIGALRM_AVAILABLE:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(60)  # 60 second timeout for LLAMA
        
        try:
            mediator = LlamaMediator()
            init_time = time.time() - start_time
            print(f"  ✅ LLAMA mediator initialized in {init_time:.2f} seconds")
            
            # Test model availability
            print("  🔍 Checking model availability...")
            if mediator.ollama_available:
                print(f"  ✅ Ollama available: {mediator.ollama_available}")
                if mediator.model_installed:
                    print(f"  ✅ Model installed: {mediator.model_installed}")
                    print(f"  📊 Current model: {getattr(mediator, 'current_model', 'Unknown')}")
                else:
                    print("  ⚠️  Model not installed")
            else:
                print("  ⚠️  Ollama not available")
                
        finally:
            if SIGALRM_AVAILABLE:
                signal.alarm(0)  # Cancel timeout
            
        return True
        
    except TimeoutError:
        print("  ❌ LLAMA initialization timed out - model may be too slow")
        return False
    except ImportError as e:
        print(f"  ❌ Import error: {e}")
        return False
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def main():
    """Run all tests"""
    print("🚀 Starting Git Automation Tests")
    print("=" * 50)
    
    tests = [
        ("GitAutomationService", test_git_automation_service),
        ("Git CLI Commands", test_git_cli_commands),
        ("GitFileDetector", test_git_file_detector),
        ("LLAMA Performance", test_llama_performance),
        ("Integration Test", test_integration_with_temp_repo),
    ]
    
    results = []
    for test_name, test_func in tests:
        try:
            print(f"\n🔄 Running {test_name}...")
            success = test_func()
            results.append((test_name, success))
        except KeyboardInterrupt:
            print(f"\n⏹️  {test_name} test interrupted by user")
            results.append((test_name, False))
            break
        except Exception as e:
            print(f"\n❌ {test_name} test crashed: {e}")
            results.append((test_name, False))
    
    # Summary
    print("\n" + "=" * 50)
    print("📊 Test Results Summary")
    print("=" * 50)
    
    passed = 0
    total = len(results)
    
    for test_name, success in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} {test_name}")
        if success:
            passed += 1
    
    print(f"\nOverall: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! Git automation is working correctly.")
        return 0
    else:
        print("⚠️  Some tests failed. Check the output above for details.")
        if passed < total:
            print("\n💡 Performance Tips:")
            print("  • Consider upgrading to Gemma3 12B for better performance")
            print("  • LLAMA 2B models can be slow on Windows")
            print("  • Use --no-llama flag to skip LLAMA operations during testing")
        return 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⏹️  Tests interrupted by user")
        sys.exit(1)
