// Cross-implementation check: run the Swift engine over the sample archives and print a
// canonical digest that the Python reference must reproduce exactly.
//
// Two implementations of an undocumented binary format WILL drift, and the failure mode
// established in FINDINGS is silent — a wrong stroke imports cleanly and never draws. This
// harness is the thing that notices. It is not part of the app target.
//
//   swiftc -O ios/InkRef/Engine/*.swift ios/Tools/CrossCheck.swift -o /tmp/crosscheck
//   /tmp/crosscheck samples/test.goodnotes
//   /tmp/crosscheck --beautify balanced in.goodnotes out.goodnotes

import Foundation

func digest(_ path: String) throws {
    let doc = try GoodNotesDocument.open(URL(fileURLWithPath: path))
    print("document \(URL(fileURLWithPath: path).lastPathComponent) "
          + "schema=\(doc.schema.map(String.init) ?? "?") pages=\(doc.pages.count)")
    for page in doc.pages {
        let strokes = try page.drawableStrokes()
        print("page \(page.id) strokes=\(strokes.count)")
        for s in strokes {
            print(String(format: "  %3d %10.4f %10.4f %10.4f %10.4f w=%.4f #%02x%02x%02x segs=%d",
                         s.index, s.box.x0, s.box.y0, s.box.x1, s.box.y1, s.width,
                         Int((s.red * 255).rounded()), Int((s.green * 255).rounded()),
                         Int((s.blue * 255).rounded()), s.segments.count))
        }
    }
}

/// A canonical dump of what the layout engine decided, for the Python side to match.
///
/// Reading the same bytes identically is necessary but not sufficient: the two engines also
/// have to *decide* the same thing, or the iPad app and the CLI quietly diverge on the same
/// document. This prints structure and plan, not just geometry.
func layoutDigest(_ path: String, _ strengthName: String) throws {
    guard let s = InkLayout.Strength.named(strengthName) else {
        throw GNError.format("unknown strength \(strengthName)")
    }
    let doc = try GoodNotesDocument.open(URL(fileURLWithPath: path))
    print("layout \(URL(fileURLWithPath: path).lastPathComponent) strength=\(s.name)")
    for page in doc.pages {
        let strokes = try page.drawableStrokes()
        guard strokes.count >= 2 else { continue }
        let boxes = strokes.map(\.box)
        let a = InkLayout.analyze(boxes)
        let (offsets, used, hurt) = InkLayout.verifiedPlan(a, boxes: boxes, strength: s)
        let nonzero = offsets.filter { !$0.isZero }.count
        let maxShift = offsets.map(\.magnitude).max() ?? 0
        print(String(format: "page %@ strokes=%d refh=%.4f pitch=%.4f cols=%d blocks=%d lines=%d",
                     page.id, boxes.count, a.refH, a.pitch,
                     a.columns.count, a.blocks.count, a.lines.count))
        print(String(format: "  plan used=%@ declined=%@ moved=%d maxshift=%.4f",
                     used?.name ?? "none", hurt ?? "-", nonzero, maxShift))
        for (k, line) in a.lines.enumerated() {
            print(String(format: "  L%d b=%d t=%d base=%.4f x0=%.4f lx=%.4f w=%d",
                         k, line.block, line.isText ? 1 : 0, line.baseline,
                         line.box.x0, line.levelX, line.words.count))
        }
    }
}

func beautify(_ input: String, _ output: String, _ strengthName: String) async throws {
    guard let strength = InkLayout.Strength.named(strengthName) else {
        throw GNError.format("unknown strength \(strengthName)")
    }
    let doc = try GoodNotesDocument.open(URL(fileURLWithPath: input))
    let result = await Beautifier.plan(document: doc, strength: strength, analyzer: nil)
    try Beautifier.apply(result, to: doc)
    try doc.write(to: URL(fileURLWithPath: output))
    for page in result.pages where !page.strokes.isEmpty {
        print(String(format: "page %@ strokes=%d lines=%d words=%d moved=%d maxshift=%.2f",
                     String(page.pageId.prefix(8)), page.strokes.count,
                     page.analysis.lines.count, page.analysis.words.count,
                     page.movedCount, page.maxShift))
        print(String(format: "  baseline %.3f->%.3f  pitch %.3f->%.3f  margin %.3f->%.3f  gap %.3f->%.3f",
                     page.before.baselineSpread, page.after.baselineSpread,
                     page.before.pitchSpread, page.after.pitchSpread,
                     page.before.marginSpread, page.after.marginSpread,
                     page.before.gapSpread, page.after.gapSpread))
    }
    print("wrote \(output)")
}

/// The recognition bridge, fed fixed input so the recogniser itself is not under test.
///
/// Vision reads a page differently on a different OS version, so demanding the two engines
/// transcribe alike would be a test of Apple. What must agree is everything between the
/// recogniser and the planner — de-duplication, stacked-line merging, which strokes each
/// word claims, and the analysis built from them. That code is pure arithmetic on boxes,
/// it has no test the app itself would fail, and it is exactly where a silent divergence
/// between the CLI and the iPad would live.
///
/// Reads the same JSON the Python side generates: {"boxes": [[x0,y0,x1,y1]...],
/// "lines": [{"text","box","words":[{"text","box","confidence"}],"confidence"}]}.
func recognitionDigest(_ path: String) throws {
    struct Word: Decodable { let text: String; let box: [Double]; let confidence: Double }
    struct Line: Decodable {
        let text: String; let box: [Double]; let words: [Word]; let confidence: Double
    }
    struct Input: Decodable { let boxes: [[Double]]; let lines: [Line] }

    let input = try JSONDecoder().decode(Input.self,
                                         from: Data(contentsOf: URL(fileURLWithPath: path)))
    func box(_ v: [Double]) -> InkBox { InkBox(x0: v[0], y0: v[1], x1: v[2], y1: v[3]) }
    let boxes = input.boxes.map(box)
    let lines = input.lines.map { l in
        RecognizedLine(text: l.text, box: box(l.box),
                       words: l.words.map {
                           RecognizedWord(text: $0.text, box: box($0.box),
                                          confidence: $0.confidence)
                       },
                       confidence: l.confidence)
    }

    let merged = Recognition.mergeStacked(Recognition.dedupe(lines))
    let (groups, unmatched) = StrokeMapper.map(merged, boxes: boxes)
    print("recognition lines=\(merged.count) groups=\(groups.count) "
          + "unmatched=\(unmatched.count)")
    for g in groups {
        print(String(format: "  %-24s %-16s %s",
                     (g.text as NSString).utf8String!,
                     (g.indices.map(String.init).joined(separator: ",") as NSString).utf8String!,
                     (String(format: "%.4f %.4f %.4f %.4f", g.box.x0, g.box.y0,
                             g.box.x1, g.box.y1) as NSString).utf8String!))
    }
    print("  unmatched \(unmatched.map(String.init).joined(separator: ","))")

    let a = StrokeMapper.analysis(groups, boxes: boxes)
    // Roles cycle so that equation/diagram (frozen) and unknown (unnamed) both appear;
    // without them the collision gate below never exercises its protected-ink branch.
    let roleCycle: [Role] = [.paragraph, .heading, .equation, .unknown, .bullet, .diagram]
    let roles = (0..<a.lines.count).map { roleCycle[$0 % roleCycle.count] }
    print(String(format: "analysis lines=%d refH=%.4f pitch=%.4f blocks=%d wordGap=%.4f",
                 a.lines.count, a.refH, a.pitch, a.blocks.count, a.wordGap))
    for (k, line) in a.lines.enumerated() {
        print(String(format: "  L%02d base=%.4f level=%d block=%d rigid=%d words=%d",
                     k, line.baseline, line.level, line.block, line.rigid ? 1 : 0,
                     line.words.count))
    }
    let planned = InkLayout.plan(a, strength: .balanced, roles: roles, skip: ["line"])
    let (constrained, gate) = Collide.constrain(a, boxes: boxes, offsets: planned,
                                                roles: roles, page: nil)
    print("gate groups=\(gate.groups) reduced=\(gate.reduced) cancelled=\(gate.cancelled)")
    for (k, o) in constrained.enumerated() where !o.isZero {
        print(String(format: "  offset %d %.4f %.4f", k, o.dx, o.dy))
    }
}

@main
struct CrossCheck {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        do {
            switch args.first {
            case "--selfcheck":
                #if DEBUG
                let failures = InkLayoutSelfCheck.run()
                failures.forEach { print("FAIL \($0)") }
                print(failures.isEmpty ? "layout self-check passed"
                                       : "\(failures.count) failures")
                exit(failures.isEmpty ? 0 : 1)
                #else
                print("build with -DDEBUG to run the self-check")
                exit(2)
                #endif
            case "--layout" where args.count == 3:
                try layoutDigest(args[2], args[1])
            case "--beautify" where args.count == 4:
                try await beautify(args[2], args[3], args[1])
            case "--recognition" where args.count == 2:
                try recognitionDigest(args[1])
            case .some(let path):
                try digest(path)
            case nil:
                print("usage: crosscheck <file.goodnotes>"
                      + " | --beautify <strength> <in> <out>"
                      + " | --recognition <input.json> | --selfcheck")
                exit(2)
            }
        } catch {
            FileHandle.standardError.write(Data("error: \(error)\n".utf8))
            exit(1)
        }
    }
}
