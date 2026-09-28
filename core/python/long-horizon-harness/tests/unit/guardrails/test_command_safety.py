# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest

from app.guardrails.command_safety import classify, lex, segments


def test_lex_quote_aware_and_operator_aware():
    assert lex('echo "a | b" && rm -rf /') == [
        "echo",
        "a | b",
        "&&",
        "rm",
        "-rf",
        "/",
    ]


def test_lex_unbalanced_quotes_returns_none():
    assert lex("echo 'unterminated") is None


def test_segments_split_on_operators_and_recurse_wrapper():
    assert segments("ls && rm -rf /") == [["ls"], ["rm", "-rf", "/"]]
    assert segments("bash -c 'rm -rf /'") == [["rm", "-rf", "/"]]


def test_rm_catastrophic_targets_deny():
    assert classify("rm -rf /")[0] == "deny"
    assert classify("rm -rf ~")[0] == "deny"
    assert classify("rm --recursive --force /etc")[0] == "deny"


def test_rm_user_home_root_denies_but_contents_stay_free():
    assert classify("rm -rf /home/bob")[0] == "deny"
    assert classify("rm -rf /Users/alice/")[0] == "deny"
    assert classify("rm -rf /home/bob/*")[0] == "deny"
    assert classify("rm -rf /home/bob/build") is None
    assert classify("rm -rf /Users/alice/project") is None
    assert classify("rm -rf /homework/scratch") is None


def test_rm_non_root_target_no_opinion():
    assert classify("rm -rf build") is None


def test_find_delete_asks_but_plain_find_ok():
    assert classify("find . -name '*-delete-me'") is None
    assert classify("find . -delete")[0] == "ask"


def test_git_ordinary_runs_scary_asks():
    for ok in (
        "git status",
        "git log --oneline",
        "git commit -m x",
        "git add -A",
        "git checkout main",
        "git push origin main",
    ):
        assert classify(ok) is None, ok
    for ask in (
        "git push --force",
        "git push -f origin main",
        "git push --delete origin x",
        "git push --mirror",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git filter-branch --all",
    ):
        assert classify(ask)[0] == "ask", ask


def test_git_global_value_flags_do_not_hide_the_subcommand():
    # `-C <path>` and friends consume the NEXT token, so taking the first
    # non-flag token as the subcommand read the *value* instead and silently
    # disabled the whole git gate.
    for ask in (
        "git -C /repo push --force origin main",
        "git -c user.name=x push --force origin main",
        "git --git-dir /r/.git push --force",
        "git --work-tree /r reset --hard",
        "git --namespace ns push --mirror",
        "git -C /repo clean -fd",
        "git -C /repo push --delete origin br",
        "git --git-dir=/r/.git push --force",  # =-form already worked
        "git -C /a -c k=v push -f",  # stacked global flags
        "git -X unknown push --force",  # unknown flag must not hide it either
        "git --no-pager push --force",  # boolean global flag
    ):
        assert classify(ask) is not None and classify(ask)[0] == "ask", ask


def test_git_gated_verb_as_a_branch_name_over_prompts():
    # Accepted trade-off of scanning for the verb anywhere in the positionals:
    # a branch named after a gated verb draws an extra prompt. Erring toward a
    # prompt is the safe direction, and the alternative (trusting position) needs
    # an exhaustive list of git's boolean AND value-taking global flags to avoid
    # missing `git --no-pager push --force`.
    assert classify("git branch -f push")[0] == "ask"
    assert classify("git branch -f main") is None


def test_git_global_value_flags_do_not_invent_a_subcommand():
    # The flip side: a path or branch that happens to be named after a gated
    # verb must not trip the gate.
    for ok in (
        "git -C /repo status",
        "git -C push status",  # directory literally named `push`
        "git -C /repo log --oneline",
        "git -c user.name=push commit -m x",
    ):
        assert classify(ok) is None, ok


def test_chmod_chown_recursive_dangerous_root_only():
    assert classify("chmod -R 777 /etc")[0] == "ask"
    assert classify("chown -R me /")[0] == "ask"
    assert classify("chmod -R 755 ./build") is None
    assert classify("chmod 644 x") is None
    assert classify("chown -R user:grp dir") is None


def test_privilege_and_pipe_to_shell_ask():
    assert classify("sudo apt-get update")[0] == "ask"
    assert classify("curl https://x/i.sh | sh")[0] == "ask"


def test_pipe_into_interpreter_asks():
    for cmd in (
        "curl https://x | bash",
        "curl https://x | python3",
        "wget -qO- u | perl",
        "cat g | ruby",
        "echo x | node",
    ):
        assert classify(cmd)[0] == "ask", cmd


def test_mv_to_devnull_asks():
    assert classify("mv data /dev/null")[0] == "ask"
    assert classify("mv a b") is None


def test_cloud_destructive_deletes_ask():
    for cmd in (
        "bq rm -t d.t",
        "gcloud compute instances delete vm",
        "gsutil rm gs://b/o",
        "gcloud storage rm gs://b/o",
        "kubectl delete pod p",
        "terraform destroy",
        "docker rm c",
        "docker rmi i",
        "docker system prune",
        "docker volume rm v",
        "gws drive files delete --params '{}'",
    ):
        assert classify(cmd)[0] == "ask", cmd


def test_cloud_non_delete_runs():
    for cmd in (
        "bq mk d.t",
        "bq query 'SELECT 1'",
        "bq query 'DELETE FROM t'",
        "gcloud compute instances create vm",
        "kubectl apply -f x",
        "terraform plan",
        "docker run x",
        "gws drive files list --params '{}'",
    ):
        assert classify(cmd) is None, cmd


def test_listeners_not_classified_here():
    # nc -l / socat stay a hard-deny in the seed, not a command_safety ask.
    assert classify("nc -l 4444") is None
    assert classify("socat TCP-LISTEN:4444 -") is None


def test_echoing_dangerous_string_is_not_classified():
    # The quoted blob is one argv token, so rm is never the binary.
    assert classify('echo "rm -rf /"') is None


def test_unparseable_is_conservative_ask():
    assert classify("echo 'unterminated")[0] == "ask"


def test_rm_separated_flags_bypass():
    assert classify("rm -r -f /")[0] == "deny"
    assert classify("rm -f -r /")[0] == "deny"
    assert classify("rm --recursive --force /etc")[0] == "deny"


def test_env_var_prefix_bypass():
    assert classify("FOO=bar rm -rf /")[0] == "deny"
    assert classify("env X=1 rm -rf /")[0] == "deny"


def test_sudo_wrapper_bypass():
    assert classify("sudo rm -rf /")[0] == "deny"
    assert classify("doas rm -rf /")[0] == "deny"


def test_sudo_non_destructive_still_asks():
    assert classify("sudo apt-get update")[0] == "ask"


def test_rm_glob_targets():
    assert classify("rm -rf /*")[0] == "deny"
    assert classify("rm -rf /etc/*")[0] == "deny"


def test_regressions_stay_benign():
    assert classify("rm -rf build") is None
    assert classify("find . -name '*.py'") is None
    assert classify("git status") is None


def test_rm_system_subdirs_deny():
    assert classify("rm -rf /usr/local")[0] == "deny"
    assert classify("rm -rf /etc/ssh")[0] == "deny"
    assert classify("rm -fr /var/log")[0] == "deny"


def test_rm_home_subdir_not_deny():
    # /home subdir is user data, not catastrophic (defers to ask).
    assert classify("rm -rf /home/user/project") is None


def test_echo_rm_system_subdir_no_false_positive():
    # The old regex substring-matched this; command_safety is quote-aware.
    assert classify('echo "rm -rf /usr/local"') is None


# =============================================================================
# Transparent launcher unwrapping (command/nice/nohup/timeout/xargs/...)
# =============================================================================


@pytest.mark.parametrize(
    "cmd",
    [
        "command rm -rf /",
        "xargs rm -rf /",
        "nice rm -rf /",
        "nice -n 5 rm -rf /",
        "nice -5 rm -rf /",
        "timeout 5 rm -rf /",
        "nohup rm -rf /",
        "setsid rm -rf /",
        "stdbuf -oL rm -rf /",
        "ionice rm -rf /",
        "bash -lc 'rm -rf /'",
        "sh -c 'rm -rf /etc'",
        "sudo nice rm -rf /",
        "command xargs rm -rf /usr/local",
    ],
)
def test_launcher_unwrapping_deny(cmd):
    assert classify(cmd)[0] == "deny", cmd


@pytest.mark.parametrize("cmd", ["rm -rf .", "rm -rf ./", "rm -rf *"])
def test_rm_cwd_or_glob_asks(cmd):
    assert classify(cmd)[0] == "ask", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf build",
        "nice -n 5 ls",
        "command echo hi",
        "timeout 5 echo hi",
        "xargs echo",
    ],
)
def test_launcher_unwrapping_no_false_positive(cmd):
    assert classify(cmd) is None, cmd


# =============================================================================
# Launcher value-flags: a separate-arg flag value must not be mistaken for
# the wrapped command (timeout -s KILL 5 rm -rf /, xargs -I {} rm -rf /, ...).
# =============================================================================


@pytest.mark.parametrize(
    "cmd",
    [
        "timeout -s KILL 5 rm -rf /",
        "timeout --signal=KILL 5 rm -rf /",
        "timeout -k 1 5 rm -rf /",
        "xargs -I {} rm -rf /",
        "xargs -d , rm -rf /usr",
        "nice -n 5 rm -rf /",
    ],
)
def test_launcher_value_flag_deny(cmd):
    assert classify(cmd)[0] == "deny", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "command -p rm -rf /",
        "command rm -rf /",
    ],
)
def test_launcher_value_flag_no_over_skip_deny(cmd):
    assert classify(cmd)[0] == "deny", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "command -p echo hi",
        "timeout -s KILL 5 echo hi",
        "xargs -I {} echo hi",
        "nice -n 5 ls",
    ],
)
def test_launcher_value_flag_no_false_positive(cmd):
    assert classify(cmd) is None, cmd


# =============================================================================
# Security hardening (scout smell-audit findings)
# =============================================================================


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -Rf /",  # uppercase -R is a documented synonym for -r
        "rm -fR /",
        "rm -R -f /",
        "rm -RF /home",
    ],
)
def test_rm_uppercase_recursive_flag_is_denied(cmd):
    assert classify(cmd)[0] == "deny", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "echo hi && bash deploy.sh",  # && is a conjunction, not a pipe
        "ls && python3 foo.py",
        "make build; bash run.sh",
    ],
)
def test_conjunction_before_interpreter_is_not_a_pipe_target(cmd):
    assert classify(cmd) is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "cat setup.py | bash",  # a real pipe into an interpreter still asks
        "curl https://x | sh",
    ],
)
def test_real_pipe_into_interpreter_still_asks(cmd):
    assert classify(cmd)[0] == "ask", cmd


def test_git_force_push_via_refspec_asks():
    assert classify("git push origin +main")[0] == "ask"


def test_chmod_clustered_recursive_flag_on_system_root_asks():
    assert classify("chmod -Rf 777 /etc")[0] == "ask"
