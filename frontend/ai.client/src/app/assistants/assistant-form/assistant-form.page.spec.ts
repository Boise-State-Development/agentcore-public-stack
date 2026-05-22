import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideRouter } from '@angular/router';
import { ReactiveFormsModule } from '@angular/forms';
import { signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { AssistantFormPage } from './assistant-form.page';
import { AssistantService } from '../services/assistant.service';
import { DocumentService } from '../services/document.service';
import { FileSourceService } from '../services/file-source.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ThemeService } from '../../components/topnav/components/theme-toggle/theme.service';

describe('AssistantFormPage', () => {
  let component: AssistantFormPage;
  let fixture: ComponentFixture<AssistantFormPage>;

  const mockAssistantService = {
    getAssistantById: vi.fn().mockReturnValue(null),
    getAssistant: vi.fn().mockResolvedValue(null),
    createAssistant: vi.fn().mockResolvedValue({}),
    updateAssistant: vi.fn().mockResolvedValue({}),
    createDraft: vi.fn().mockResolvedValue({}),
  };

  const mockDocumentService = {
    listDocuments: vi.fn().mockResolvedValue({ documents: [] }),
    requestUploadUrl: vi.fn(),
    uploadToS3: vi.fn(),
    deleteDocument: vi.fn(),
    pollDocumentStatus: vi.fn(),
  };

  const mockFileSourceService = {
    listFileSources: vi.fn().mockResolvedValue([]),
  };

  const mockSidenavService = {
    hide: vi.fn(),
    show: vi.fn(),
  };

  const mockThemeService = {
    theme: signal('light'),
  };

  const mockDialog = {
    open: vi.fn(),
  };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();

    await TestBed.configureTestingModule({
      imports: [ReactiveFormsModule],
      providers: [
        provideRouter([]),
        { provide: AssistantService, useValue: mockAssistantService },
        { provide: DocumentService, useValue: mockDocumentService },
        { provide: FileSourceService, useValue: mockFileSourceService },
        { provide: SidenavService, useValue: mockSidenavService },
        { provide: ThemeService, useValue: mockThemeService },
        { provide: Dialog, useValue: mockDialog },
      ],
    })
      .overrideComponent(AssistantFormPage, {
        set: {
          // Minimal template to avoid pulling in child components
          template: '<div></div>',
        },
      })
      .compileComponents();

    fixture = TestBed.createComponent(AssistantFormPage);
    component = fixture.componentInstance;
    component.ngOnInit();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('live form signal sync', () => {
    it('should initialize live signals with empty defaults', () => {
      expect(component.liveFormName()).toBe('');
      expect(component.liveFormDescription()).toBe('');
      expect(component.liveFormInstructions()).toBe('');
      expect(component.liveFormEmoji()).toBe('');
      expect(component.liveFormStarters()).toEqual([]);
    });

    it('should sync name signal when form control changes', () => {
      component.form.get('name')!.setValue('Test Assistant');
      expect(component.liveFormName()).toBe('Test Assistant');
    });

    it('should sync description signal when form control changes', () => {
      component.form.get('description')!.setValue('A helpful test assistant');
      expect(component.liveFormDescription()).toBe('A helpful test assistant');
    });

    it('should sync instructions signal when form control changes', () => {
      const instructions = 'You are a helpful assistant that answers questions about testing.';
      component.form.get('instructions')!.setValue(instructions);
      expect(component.liveFormInstructions()).toBe(instructions);
    });

    it('should sync emoji signal when form control changes', () => {
      component.form.get('emoji')!.setValue('🤖');
      expect(component.liveFormEmoji()).toBe('🤖');
    });

    it('should sync starters signal when starters are added', () => {
      component.addStarter();
      component.starters.at(0).setValue('Hello, how can I help?');
      expect(component.liveFormStarters()).toEqual(['Hello, how can I help?']);
    });

    it('should sync starters signal when starters are removed', () => {
      component.addStarter();
      component.addStarter();
      component.starters.at(0).setValue('Starter 1');
      component.starters.at(1).setValue('Starter 2');
      expect(component.liveFormStarters()).toEqual(['Starter 1', 'Starter 2']);

      component.removeStarter(0);
      expect(component.liveFormStarters()).toEqual(['Starter 2']);
    });

    it('should update all signals on patchValue', () => {
      component.form.patchValue({
        name: 'Patched Name',
        description: 'Patched description text',
        instructions: 'Patched instructions for the assistant',
        emoji: '🚀',
      });

      expect(component.liveFormName()).toBe('Patched Name');
      expect(component.liveFormDescription()).toBe('Patched description text');
      expect(component.liveFormInstructions()).toBe('Patched instructions for the assistant');
      expect(component.liveFormEmoji()).toBe('🚀');
    });

    it('should reflect incremental typing in instructions', () => {
      const control = component.form.get('instructions')!;
      control.setValue('You');
      expect(component.liveFormInstructions()).toBe('You');

      control.setValue('You are');
      expect(component.liveFormInstructions()).toBe('You are');

      control.setValue('You are a helpful assistant');
      expect(component.liveFormInstructions()).toBe('You are a helpful assistant');
    });
  });

  describe('lifecycle', () => {
    it('should hide sidenav on init', () => {
      expect(mockSidenavService.hide).toHaveBeenCalled();
    });

    it('should show sidenav on destroy', () => {
      component.ngOnDestroy();
      expect(mockSidenavService.show).toHaveBeenCalled();
    });

    it('should not leak subscriptions after destroy', () => {
      const control = component.form.get('name')!;
      component.ngOnDestroy();

      // After destroy, changing the form should NOT update the signal
      const valueBefore = component.liveFormName();
      control.setValue('After destroy');
      expect(component.liveFormName()).toBe(valueBefore);
    });
  });
});
