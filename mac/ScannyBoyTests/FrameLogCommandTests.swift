import Foundation
import Testing

@testable import ScannyBoy

/// The `framelog` argument lists must match `shared/contract/CONTRACT.md`,
/// stay out of the resident helper, and carry the token only in the
/// environment.
@Suite("Frame log command construction")
struct FrameLogCommandTests {
    private static let roll = URL(filePath: "/Volumes/Scans/roll-12")
    private static let baseURL = "https://lonnie-smith.com"

    @Test("framelog rolls passes the status and base URL, and no token")
    func rollsArguments() {
        let command = CLICommand.framelogRolls(status: "active", baseURL: Self.baseURL)
        #expect(
            command.arguments == [
                "framelog", "rolls",
                "--status", "active",
                "--base-url", "https://lonnie-smith.com",
            ])
        #expect(command.environment.isEmpty)
    }

    @Test("framelog roll passes the roll folder and the ref")
    func rollArguments() {
        let command = CLICommand.framelogRoll(
            roll: Self.roll, ref: "k3j9x0a1bc", baseURL: Self.baseURL)
        #expect(
            command.arguments == [
                "framelog", "roll",
                "--roll", "/Volumes/Scans/roll-12",
                "--ref", "k3j9x0a1bc",
                "--base-url", "https://lonnie-smith.com",
            ])
    }

    @Test("framelog apply sends assignments as sorted JSON, with null for no frame")
    func applyArguments() {
        let command = CLICommand.framelogApply(
            roll: Self.roll,
            ref: "k3j9x0a1bc",
            assignments: ["n3": nil, "n1": 1, "n2": 5],
            baseURL: Self.baseURL
        )
        #expect(
            command.arguments == [
                "framelog", "apply",
                "--roll", "/Volumes/Scans/roll-12",
                "--ref", "k3j9x0a1bc",
                "--assignments", #"{"n1":1,"n2":5,"n3":null}"#,
                "--base-url", "https://lonnie-smith.com",
            ])
    }

    @Test("empty assignments serialise to an empty JSON object")
    func emptyAssignments() {
        #expect(CLICommand.framelogAssignmentsJSON([:]) == "{}")
    }

    @Test("no framelog command is routed through the resident helper")
    func frameLogNeverRoutesThroughDaemon() {
        let commands = [
            CLICommand.framelogRolls(status: "all", baseURL: Self.baseURL),
            CLICommand.framelogRoll(roll: Self.roll, ref: "1", baseURL: Self.baseURL),
            CLICommand.framelogApply(
                roll: Self.roll, ref: "1", assignments: [:], baseURL: Self.baseURL),
        ]
        for command in commands {
            #expect(!CLIRunner.routesThroughDaemon(command))
            #expect(!CLIRunner.routesThroughCaptureDaemon(command))
        }
    }

    @Test("withEnvironment adds variables and replaces a key already present")
    func withEnvironmentMerges() {
        let command = CLICommand(arguments: ["x"], environment: ["A": "1", "B": "2"])
            .withEnvironment(["B": "3", "C": "4"])
        #expect(command.arguments == ["x"])
        #expect(command.environment == ["A": "1", "B": "3", "C": "4"])
    }

    @Test("the token reaches a one-shot child in its environment, never argv")
    func tokenReachesChildEnvironment() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let executable = try TestSupport.writeTestExecutable(
                #"echo "token=$SCANNY_BOY_FRAMELOG_TOKEN" >&2"#,
                in: directory
            )
            let token = "fl_secret12345678"
            let command = CLICommand
                .framelogRolls(status: "active", baseURL: Self.baseURL)
                .withEnvironment([FrameLogCredentials.tokenEnvironmentKey: token])
            #expect(!command.arguments.contains(where: { $0.contains(token) }))

            let runner = CLIRunner(executable: executable)
            var logLines: [String] = []
            for await output in try await runner.session(for: command).start() {
                if case .log(let line) = output {
                    logLines.append(line)
                }
            }
            #expect(logLines == ["token=\(token)"])
        }
    }
}
