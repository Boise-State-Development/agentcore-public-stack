import AgentCoreKit
import SwiftUI

/// Answer a waiting scheduled run: every prompt it waits on, sent together,
/// then how the run got on.
struct InboxReviewView: View {
    @State private var model: InboxReviewModel
    let openConversation: () -> Void
    @Environment(\.dismiss) private var dismiss

    init(model: InboxReviewModel, openConversation: @escaping () -> Void) {
        self._model = State(initialValue: model)
        self.openConversation = openConversation
    }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle(model.label ?? String(localized: "Review"))
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { toolbar }
        }
        .interactiveDismissDisabled(model.phase == .sending)
        .task {
            if model.phase == .loading {
                await model.load()
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch model.phase {
        case .loading:
            ProgressView()
        case .answering, .sending:
            PromptsForm(model: model)
        case .following:
            ContentUnavailableView {
                Label("Sent", systemImage: "paperplane")
            } description: {
                Text("The run is continuing. You can close this; the inbox shows how it ends.")
            } actions: {
                ProgressView()
            }
        case .ended(let outcome):
            outcomeView(outcome)
        case .lost:
            result(
                "No word yet", symbol: "questionmark.circle",
                detail: "The run didn't report back. Its conversation will have the result.")
        case .alreadyAnswered:
            result(
                "Already answered", symbol: "checkmark.circle",
                detail: "This was answered somewhere else before your answer arrived.")
        case .expired:
            result("Expired", symbol: "clock.badge.xmark", detail: "This expired before it was answered.")
        case .openConversationInstead:
            result(
                "Answer this one on the web", symbol: "safari",
                detail: "It needs something this app can't do yet, like a sign-in. Open it in the web app to answer.")
        case .failed(let problem):
            ContentUnavailableView {
                Label("Couldn't load this", systemImage: "exclamationmark.bubble")
            } description: {
                LoadProblemText(problem: problem)
            } actions: {
                Button("Try again") {
                    Task { await model.load() }
                }
                .buttonStyle(.borderedProminent)
            }
        }
    }

    @ViewBuilder
    private func outcomeView(_ outcome: RunStatus.Outcome) -> some View {
        switch outcome {
        case .running, .finished:
            result("Done", symbol: "checkmark.circle", detail: "The run finished. Its answer is in the conversation.")
        case .waitingAgain:
            result(
                "Waiting for you again", symbol: "exclamationmark.bubble",
                detail: "The run continued and paused on something new. It's back in your inbox.")
        case .needsConnection:
            result(
                "Connect an account", symbol: "link",
                detail: "The run needs an account connected. Open its conversation on the web to connect it.")
        case .failed(let error):
            result(
                "The run stopped", symbol: "xmark.octagon",
                detail: error ?? String(localized: "It stopped with an error. Its conversation may say more."))
        }
    }

    private func result(_ title: LocalizedStringKey, symbol: String, detail: String) -> some View {
        ContentUnavailableView {
            Label(title, systemImage: symbol)
        } description: {
            Text(detail)
        } actions: {
            Button("Open conversation", action: openConversation)
                .buttonStyle(.borderedProminent)
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        switch model.phase {
        case .answering, .sending:
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel") { dismiss() }
                    .disabled(model.phase == .sending)
            }
            ToolbarItem(placement: .confirmationAction) {
                if model.phase == .sending {
                    ProgressView()
                } else {
                    Button("Send") {
                        Task { await model.send() }
                    }
                    .disabled(!model.canSend)
                }
            }
        default:
            ToolbarItem(placement: .confirmationAction) {
                Button("Done") { dismiss() }
            }
        }
    }
}

/// Every prompt, one section or more each.
struct PromptsForm: View {
    @Bindable var model: InboxReviewModel

    var body: some View {
        Form {
            ForEach(model.prompts) { prompt in
                switch prompt.kind {
                case .toolApproval(let toolName, let message, let toolInput):
                    ApprovalSection(
                        toolName: toolName, message: message, toolInput: toolInput,
                        approved: approvalBinding(prompt.id))
                case .questions(let questions):
                    QuestionsSection(questions: questions, draft: questionsBinding(prompt.id))
                case .form(let form):
                    FormSection(form: form, draft: formBinding(prompt.id, form: form))
                case .notAnswerableHere:
                    EmptyView()
                }
            }
            if let problem = model.sendProblem {
                Section {
                    LoadProblemText(problem: problem)
                        .foregroundStyle(.red)
                }
            }
        }
        .disabled(model.phase == .sending)
    }

    private func approvalBinding(_ id: String) -> Binding<Bool?> {
        Binding {
            if case .approval(let value) = model.drafts[id] { return value }
            return nil
        } set: {
            model.drafts[id] = .approval($0)
        }
    }

    private func questionsBinding(_ id: String) -> Binding<QuestionsDraft> {
        Binding {
            if case .questions(let draft) = model.drafts[id] { return draft }
            return QuestionsDraft()
        } set: {
            model.drafts[id] = .questions($0)
        }
    }

    private func formBinding(_ id: String, form: ElicitationForm) -> Binding<FormDraft> {
        Binding {
            if case .form(let draft) = model.drafts[id] { return draft }
            return FormDraft(form: form)
        } set: {
            model.drafts[id] = .form($0)
        }
    }
}

struct ApprovalSection: View {
    let toolName: String
    let message: String?
    let toolInput: String?
    @Binding var approved: Bool?

    var body: some View {
        Section {
            if let message = message.nonEmpty {
                Text(message)
            }
            if let toolInput = toolInput.nonEmpty {
                DisclosureGroup("What it will send") {
                    ScrollView(.horizontal) {
                        Text(toolInput)
                            .font(.footnote.monospaced())
                            .textSelection(.enabled)
                    }
                }
            }
            ChoiceRow(title: "Allow", isOn: approved == true) { approved = true }
            ChoiceRow(title: "Don't allow", isOn: approved == false) { approved = false }
        } header: {
            Text("Allow \(toolName.isEmpty ? String(localized: "a tool") : toolName) to run?")
        }
    }
}

struct QuestionsSection: View {
    let questions: [Question]
    @Binding var draft: QuestionsDraft

    var body: some View {
        ForEach(Array(questions.enumerated()), id: \.offset) { index, question in
            Section {
                ForEach(question.options, id: \.label) { option in
                    ChoiceRow(
                        title: LocalizedStringKey(option.label), detail: option.description,
                        isOn: draft.isPicked(option.label, for: question)
                    ) {
                        draft.toggle(option.label, for: question)
                    }
                }
                TextField(
                    "Other", text: otherBinding(question.header), axis: .vertical)
            } header: {
                Text(question.question)
                    .textCase(nil)
            } footer: {
                if question.multiSelect {
                    Text("Choose any that apply.")
                }
                if index == questions.count - 1 {
                    Toggle("Skip these questions", isOn: $draft.skipped)
                        .padding(.top, 8)
                }
            }
        }
        .disabled(draft.skipped)
    }

    private func otherBinding(_ header: String) -> Binding<String> {
        Binding {
            draft.otherText[header] ?? ""
        } set: {
            draft.skipped = false
            draft.otherText[header] = $0
        }
    }
}

struct FormSection: View {
    let form: ElicitationForm
    @Binding var draft: FormDraft

    var body: some View {
        let problems = draft.problems(in: form)
        Section {
            if let message = form.message.nonEmpty {
                Text(message)
            }
            if form.hasUnsupportedField {
                Text("This form asks for something this app can't fill in yet. You can decline it here.")
                    .foregroundStyle(.secondary)
            }
            Toggle("Decline this form", isOn: $draft.declined)
        } header: {
            Text("\(form.serverName.nonEmpty ?? String(localized: "A connected service")) needs some details")
                .textCase(nil)
        }
        if !draft.declined && !form.hasUnsupportedField {
            ForEach(form.fields, id: \.name) { field in
                Section {
                    fieldView(field)
                } header: {
                    Text(field.required ? "\(field.title) (required)" : "\(field.title)")
                        .textCase(nil)
                } footer: {
                    VStack(alignment: .leading, spacing: 4) {
                        if let description = field.description.nonEmpty {
                            Text(description)
                        }
                        if let problem = problems[field.name] {
                            FieldProblemText(problem: problem)
                                .foregroundStyle(.red)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func fieldView(_ field: FormField) -> some View {
        switch field.kind {
        case .text(let format, _, _):
            TextField(field.title, text: textBinding(field), axis: .vertical)
                .keyboardType(format == "email" ? .emailAddress : format == "uri" ? .URL : .default)
                .textInputAutocapitalization(format == nil ? .sentences : .never)
        case .number:
            TextField(field.title, text: textBinding(field))
                .keyboardType(.decimalPad)
        case .integer:
            TextField(field.title, text: textBinding(field))
                .keyboardType(.numberPad)
        case .boolean:
            Toggle(field.title, isOn: boolBinding(field))
        case .choice(let options, _, _, _):
            ForEach(options, id: \.value) { option in
                ChoiceRow(
                    title: LocalizedStringKey(option.label),
                    isOn: draft.choices(field).contains(option.value)
                ) {
                    draft.toggle(option.value, in: field)
                }
            }
        }
    }

    private func textBinding(_ field: FormField) -> Binding<String> {
        Binding {
            draft.text(field)
        } set: {
            draft.values[field.name] = .text($0)
        }
    }

    private func boolBinding(_ field: FormField) -> Binding<Bool> {
        Binding {
            draft.bool(field)
        } set: {
            draft.values[field.name] = .bool($0)
        }
    }
}

/// A tappable row with a checkmark when chosen.
struct ChoiceRow: View {
    let title: LocalizedStringKey
    var detail: String?
    let isOn: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .foregroundStyle(.primary)
                    if let detail {
                        Text(detail)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if isOn {
                    Image(systemName: "checkmark")
                        .foregroundStyle(.tint)
                        .accessibilityHidden(true)
                }
            }
            .contentShape(.rect)
        }
        .accessibilityAddTraits(isOn ? .isSelected : [])
    }
}

/// `FieldProblem` in words.
struct FieldProblemText: View {
    let problem: FieldProblem

    var body: some View {
        switch problem {
        case .required: Text("Required.")
        case .tooShort(let count): Text("At least \(count) characters.")
        case .tooLong(let count): Text("At most \(count) characters.")
        case .notANumber: Text("Enter a number.")
        case .notAWholeNumber: Text("Enter a whole number.")
        case .belowMinimum(let minimum): Text("At least \(minimum.formatted()).")
        case .aboveMaximum(let maximum): Text("At most \(maximum.formatted()).")
        case .tooFewChoices(let count): Text("Choose at least \(count).")
        case .tooManyChoices(let count): Text("Choose at most \(count).")
        }
    }
}

#Preview("Review") {
    InboxReviewView(model: PreviewData.inbox().review(PreviewData.inboxItems[0])) {}
}
