import SwiftUI
import UniformTypeIdentifiers

/// Two screens, SPEC §6: pick a document, then compare and export. Everything the app
/// knows about the file format lives behind the engine — this file only ever sees strokes,
/// offsets and roles.
@MainActor
struct ContentView: View {
    @Bindable var vm: RefactorViewModel
    @State private var importing = false
    @State private var sharing = false
    @State private var showingSettings = false
    @AppStorage("BACKBOARD_API_KEY") private var apiKey = ""

    /// GoodNotes' UTI is imported by the app (see Info.plist), but the picker must still
    /// open a file that arrived with a generic type — an unpickable document is a dead demo.
    private static let importable: [UTType] =
        [UTType("com.goodnotes.document"), UTType.zip, UTType.data].compactMap { $0 }

    var body: some View {
        NavigationStack {
            Group {
                if vm.pages.isEmpty { importScreen } else { resultScreen }
            }
            .navigationTitle(vm.pages.isEmpty ? "" : (vm.documentName ?? "InkRef"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if !vm.pages.isEmpty {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("New document", systemImage: "doc.badge.plus") {
                            withAnimation { vm.reset() }
                        }
                    }
                }
            }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: Self.importable) { result in
            switch result {
            case let .success(url): Task { await vm.load(url) }
            case let .failure(error): vm.status = .failed(error.localizedDescription)
            }
        }
        .sheet(isPresented: $sharing) {
            if let url = vm.exportURL { ShareSheet(items: [url]) }
        }
        .sheet(isPresented: $showingSettings) {
            NavigationStack {
                ScrollView { aiSection.padding() }
                    .navigationTitle("Options")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") { showingSettings = false }
                        }
                    }
            }
            .presentationDetents([.medium, .large])
        }
        .alert("Couldn't do that", isPresented: failed) {
            Button("OK") { vm.status = .idle }
        } message: {
            Text(failureMessage)
        }
        .task {
            #if DEBUG
            // Lets a screenshot of the result screen be taken without driving the UI, which
            // is how the preview is checked from the command line. Debug builds only.
            let args = ProcessInfo.processInfo.arguments
            if args.contains("-autoDemo") {
                await vm.loadSample()
                await vm.refactor()
                // `-before` and `-structure` let each view be captured deterministically;
                // synthetic taps on the simulator are not reliable enough to drive it.
                if args.contains("-before") { vm.showRefactored = false }
                if args.contains("-structure") { vm.showStructure = true }
                // Drives the write path from the command line. The share sheet cannot be
                // automated, and the export is the one step whose output has to be audited
                // by something other than the code that produced it.
                if args.contains("-autoExport") {
                    await vm.export()
                    print("EXPORTED \(vm.exportURL?.path ?? "nil")")
                }
            }
            #endif
        }
    }

    // MARK: - screen 1: import

    private var importScreen: some View {
        ScrollView {
            VStack(spacing: 28) {
                VStack(spacing: 10) {
                    Text("InkRef")
                        .font(.system(size: 46, weight: .semibold, design: .serif))
                    Text("Prettier for handwriting. Your strokes, refactored — never redrawn.")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding(.bottom, 8)

                Button { importing = true } label: {
                    Label("Choose a GoodNotes document", systemImage: "doc.text.magnifyingglass")
                        .font(.title3.weight(.semibold))
                        .frame(maxWidth: 520, minHeight: 62)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

                Text("…or share a notebook to InkRef from GoodNotes")
                    .font(.footnote)
                    .foregroundStyle(.secondary)

                Button("try a sample page") { Task { await vm.loadSample() } }
                    .font(.callout)

                if let name = vm.documentName {
                    VStack(spacing: 4) {
                        Text(name).font(.title2.weight(.medium))
                        Text(vm.pageCount == 1 ? "1 page" : "\(vm.pageCount) pages")
                            .font(.title3)
                            .foregroundStyle(.secondary)
                    }
                    .transition(.opacity)
                }

                strengthPicker

                Button { Task { await vm.refactor() } } label: {
                    Label("Beautify Notes", systemImage: "wand.and.stars")
                        .font(.title3.weight(.semibold))
                        .frame(maxWidth: 520, minHeight: 62)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .disabled(vm.documentName == nil || vm.status == .analyzing)

                busy

                Button("Options") { showingSettings = true }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 24)
            .padding(.vertical, 48)
            .animation(.default, value: vm.documentName)
        }
    }

    private var strengthPicker: some View {
        VStack(spacing: 10) {
            Text("Formatting strength")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Picker("Formatting strength", selection: $vm.strength) {
                ForEach(InkLayout.Strength.all) { s in
                    Text(s.name.capitalized).tag(s)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 420)
        }
    }

    /// Small on purpose. The geometry engine is the product; the model only names regions,
    /// and turning it off has to look like a normal thing to do (SPEC §13, §14).
    private var aiSection: some View {
        VStack(spacing: 12) {
            Divider().frame(maxWidth: 520)
            Toggle("Read the page to find its words", isOn: $vm.readPage)
                .fixedSize()
            Text(vm.readPage
                 ? "On-device text recognition finds the lines and words; the layout engine "
                   + "then moves your original strokes. Nothing leaves the iPad."
                 : "Lines and words are inferred from stroke spacing alone — faster, and "
                   + "less reliable on cramped or mathematical writing.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)
            HStack(spacing: 24) {
                Picker("Structure AI", selection: $vm.aiMode) {
                    ForEach(AIMode.allCases, id: \.self) { mode in
                        Text(mode.rawValue.capitalized).tag(mode)
                    }
                }
                .pickerStyle(.menu)
                Toggle("Send page image", isOn: $vm.useVision)
                    .fixedSize()
                    .disabled(vm.aiMode == .off)
            }

            // An iPad has no shell, so the environment variable the CLI uses cannot exist
            // here. BackboardConfig already reads UserDefaults under the same name; this is
            // the only way to put a key there. Stored on device, never bundled, never logged.
            if vm.aiMode != .off && vm.aiMode != .heuristic {
                VStack(spacing: 6) {
                    SecureField("Backboard API key", text: $apiKey)
                        .textFieldStyle(.roundedBorder)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .frame(maxWidth: 420)
                    Text(apiKey.trimmingCharacters(in: .whitespaces).isEmpty
                         ? "No key set — falling back to on-device geometry heuristics."
                         : "Key stored on this iPad. Model: \(BackboardConfig.fromEnvironment().model)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Text("Lines, words, baselines and margins are found on device by geometry. "
                 + "AI only labels what a region is — it never decides where any ink goes. "
                 + "With it off nothing leaves the iPad; with it on the line geometry is "
                 + "sent to be labelled, and the page image only if you ask for it.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)
        }
        .padding(.top, 12)
    }

    @ViewBuilder private var busy: some View {
        switch vm.status {
        case .loading:
            ProgressView("Opening document…")
        case .analyzing:
            // Determinate where we can be. Reading a dense page takes seconds, and a bar
            // that visibly advances is the difference between "working" and "hung".
            VStack(spacing: 8) {
                if let f = vm.progressFraction {
                    ProgressView(value: f).frame(maxWidth: 320)
                } else {
                    ProgressView()
                }
                Text(vm.progress ?? "Reading the handwriting…")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .contentTransition(.opacity)
            }
            .animation(.default, value: vm.progress)
        default:
            EmptyView()
        }
    }

    // MARK: - screen 2: result

    private var resultScreen: some View {
        VStack(spacing: 0) {
            controlBar
            Divider()
            ScrollView {
                LazyVStack(spacing: 32) {
                    ForEach(vm.pages) { page in pageCard(page) }
                }
                .frame(maxWidth: .infinity)
                .padding(24)
            }
        }
    }

    private var controlBar: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 16) { leadingControls; Spacer(minLength: 12); trailingControls }
            VStack(spacing: 12) {
                HStack(spacing: 16) { leadingControls }
                HStack(spacing: 16) { trailingControls }
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(.bar)
    }

    @ViewBuilder private var leadingControls: some View {
        Picker("Comparison", selection: beforeAfter) {
            Text("Before").tag(false)
            Text("After").tag(true)
        }
        .pickerStyle(.segmented)
        .frame(width: 220)

        Toggle(isOn: $vm.showStructure) {
            Label("Analysis", systemImage: "square.dashed.inset.filled")
        }
        .toggleStyle(.button)

        // Not a debug menu buried behind a build flag. When a page comes out looking much
        // like it went in, this is the one control that says which stage decided that.
        Toggle(isOn: $vm.showRecognition) {
            Label("Reading", systemImage: "text.viewfinder")
        }
        .toggleStyle(.button)
        .disabled(vm.pages.allSatisfy(\.recognized.isEmpty))

        Picker("Strength", selection: $vm.strength) {
            ForEach(InkLayout.Strength.all) { s in
                Text(s.name.capitalized).tag(s)
            }
        }
        .pickerStyle(.menu)
        .onChange(of: vm.strength) { Task { await vm.refactor() } }
    }

    @ViewBuilder private var trailingControls: some View {
        if vm.status == .analyzing { ProgressView().controlSize(.small) }

        Button("Undo") { withAnimation(.beautify) { vm.showRefactored = false } }
            .disabled(!vm.showRefactored)

        Button {
            Task {
                await vm.export()
                sharing = vm.exportURL != nil
            }
        } label: {
            Label(vm.isExporting ? "Applying…" : "Apply & open in GoodNotes",
                  systemImage: "square.and.arrow.up")
                .font(.headline)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .disabled(vm.isExporting || vm.status == .analyzing)
    }

    private func pageCard(_ page: PagePreview) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            // On device this is the only place the stage timings can be read, and a
            // screenshot has to be able to carry them off the iPad.
            if !vm.timings.isEmpty, page.id == vm.pages.first?.id {
                Text(vm.timings.summary)
                    .font(.caption.monospaced())
                    .foregroundStyle(.tertiary)
            }
            Text(page.caption)
                .font(.footnote)
                .foregroundStyle(.secondary)

            PreviewCanvas(strokes: page.strokes, offsets: page.offsets,
                          analysis: page.analysis, roles: page.roles,
                          showStructure: vm.showStructure,
                          recognized: page.recognized, groups: page.groups,
                          unmatched: page.unmatched, showRecognition: vm.showRecognition,
                          progress: vm.showRefactored ? 1 : 0,
                          paperSize: page.paperSize, background: page.background)
                .padding(10)
                // A document preview is a sheet of paper in either colour scheme; dark-mode
                // black ink on a dark ground would be invisible.
                .background(Color.white, in: RoundedRectangle(cornerRadius: 14))
                .shadow(color: .black.opacity(0.28), radius: 20, y: 8)
                .animation(.easeInOut(duration: 0.25), value: vm.showStructure)

            metrics(page)
        }
        .frame(maxWidth: 980)
    }

    private static let metricRows: [(String, KeyPath<LayoutMetrics, Double>)] = [
        ("baseline wobble", \.baselineSpread), ("line pitch", \.pitchSpread),
        ("left margin", \.marginSpread), ("word gaps", \.gapSpread)]

    private func metrics(_ page: PagePreview) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 6) {
            GridRow {
                Text("irregularity"); Text("before"); Text("after"); Text("change")
            }
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)

            ForEach(Self.metricRows.indices, id: \.self) { i in
                let (label, key) = Self.metricRows[i]
                let gain = page.improvement[keyPath: key]
                GridRow {
                    Text(label)
                    Text(String(format: "%.2f pt", page.before[keyPath: key]))
                    Text(String(format: "%.2f pt", page.after[keyPath: key]))
                    Text(String(format: "%+.0f%%", gain * 100))
                        .foregroundStyle(gain > 0.01 ? Color.green : .secondary)
                }
            }
            .font(.callout.monospacedDigit())
        }
    }

    // MARK: - bindings

    private var beforeAfter: Binding<Bool> {
        Binding(get: { vm.showRefactored },
                set: { on in withAnimation(.beautify) { vm.showRefactored = on } })
    }

    private var failed: Binding<Bool> {
        Binding(get: { if case .failed = vm.status { return true }; return false },
                set: { if !$0 { vm.status = .idle } })
    }

    private var failureMessage: String {
        if case let .failed(message) = vm.status { return message }
        return ""
    }
}

#Preview {
    ContentView(vm: RefactorViewModel())
}
