import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ConversationList } from './conversation-list';

describe('ConversationList', () => {
  let component: ConversationList;
  let fixture: ComponentFixture<ConversationList>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ConversationList]
    })
    .compileComponents();

    fixture = TestBed.createComponent(ConversationList);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
